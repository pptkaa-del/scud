"""Unit-тесты CommandRepository — `update_status_to_executed`/`_failed`
проверяют locker_id в WHERE для защиты от cross-locker spoofing
(скомпрометированное устройство A не может пометить команду устройства B
как executed/failed)."""

import asyncio
from datetime import datetime
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

import psycopg2

from app.locker_data.db.models.command import CommandStatus, CommandType
from app.locker_data.db.repositories.command import _ACTIVE_STATUSES, CommandRepository

_LOCKER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
_LOCKER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
_COMMAND_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def _make_command_row(locker_id: str, status: str = "executed") -> dict:
    """Минимальный row для from_dict(LockerCommand, ...)."""
    return {
        "id": _COMMAND_ID,
        "locker_id": locker_id,
        "type": CommandType.OPEN,
        "status": CommandStatus(status),
        "parameters": {},
        "created_at": datetime.now(),
        "created_by": 42,
        "sent_at": None,
        "delivered_at": None,
        "executed_at": datetime.now(),
        "failed_at": None,
        "result": {},
        "error_message": None,
        "retry_count": 0,
        "max_retries": 3,
    }


class TestUpdateStatusToSentClaim(unittest.TestCase):
    """update_status_to_sent — claim-based CAS (COR-02/18): переход pending→sent
    выполняется только если строка ещё pending. Это закрывает (а) регрессию
    executed→sent (запоздавший inline-апдейт не откатит уже executed-команду) и
    (б) double-send (только один writer из inline+processor успешно заявит
    команду; второй получит 0 строк)."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.repo = CommandRepository(self.db_mock)

    async def _claims_pending_async(self):
        # Строка ещё pending → UPDATE ... WHERE status='pending' находит её,
        # RETURNING отдаёт обновлённую (sent) строку.
        self.db_mock.fetch_one.return_value = _make_command_row(
            _LOCKER_A, status="sent"
        )
        result = await self.repo.update_status_to_sent(_COMMAND_ID, ttl_s=60)

        sql = self.db_mock.fetch_one.call_args.args[0]
        # CAS-guard: переход только из pending.
        self.assertIn("status = 'pending'", sql)
        self.assertNotIn("retry_count = retry_count + 1", sql)
        self.assertIsNotNone(result)
        self.assertEqual(result.status, CommandStatus.SENT)

    def test_claims_pending_row(self):
        asyncio.run(self._claims_pending_async())

    async def _does_not_clobber_executed_async(self):
        # Команда уже executed (устройство ACK'нуло раньше, чем долетел
        # in-flight update_status_to_sent из inline-доставки). CAS-guard
        # status='pending' не находит строку → 0 строк → None. executed
        # НЕ откатывается обратно в sent.
        self.db_mock.fetch_one.return_value = None
        result = await self.repo.update_status_to_sent(_COMMAND_ID, ttl_s=60)
        self.assertIsNone(result)

    def test_does_not_clobber_executed_row(self):
        asyncio.run(self._does_not_clobber_executed_async())

    async def _single_claim_under_race_async(self):
        # Два writer'а (inline + queue-processor) пытаются заявить одну pending
        # команду. Первый CAS выигрывает (получает sent-строку), второй
        # промахивается (0 строк → None). Только один send_command должен
        # произойти у вызывающего кода.
        self.db_mock.fetch_one.side_effect = [
            _make_command_row(_LOCKER_A, status="sent"),
            None,
        ]
        first = await self.repo.update_status_to_sent(_COMMAND_ID, ttl_s=60)
        second = await self.repo.update_status_to_sent(_COMMAND_ID, ttl_s=60)
        self.assertIsNotNone(first)
        self.assertIsNone(second)

    def test_only_one_writer_claims_under_race(self):
        asyncio.run(self._single_claim_under_race_async())

    async def _does_not_increment_retry_count_async(self):
        # retry_count декуплен от sent: успешная отправка НЕ жжёт retry-бюджет.
        self.db_mock.fetch_one.return_value = _make_command_row(
            _LOCKER_A, status="sent"
        )
        await self.repo.update_status_to_sent(_COMMAND_ID, ttl_s=60)
        sql = self.db_mock.fetch_one.call_args.args[0]
        self.assertNotIn("retry_count", sql)

    def test_does_not_increment_retry_count(self):
        asyncio.run(self._does_not_increment_retry_count_async())


class TestUpdateStatusToExecutedLockerScope(unittest.TestCase):
    """update_status_to_executed обновляет команду только если она принадлежит
    указанному locker_id (cross-locker spoofing защита)."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.repo = CommandRepository(self.db_mock)

    async def _matching_scope_async(self):
        self.db_mock.fetch_one.return_value = _make_command_row(_LOCKER_A)
        result = await self.repo.update_status_to_executed(
            _COMMAND_ID, _LOCKER_A, {"output": "ok"}
        )

        # SQL проверяет AND locker_id = %s
        sql = self.db_mock.fetch_one.call_args.args[0]
        self.assertIn("AND locker_id = %s", sql)
        # params: (json_result, command_id, locker_id)
        params = self.db_mock.fetch_one.call_args.args[1]
        self.assertEqual(params[1], _COMMAND_ID)
        self.assertEqual(params[2], _LOCKER_A)
        # Команда возвращается
        self.assertIsNotNone(result)
        self.assertEqual(result.locker_id, _LOCKER_A)

    def test_executed_with_matching_locker_id_updates_and_returns_command(self):
        asyncio.run(self._matching_scope_async())

    async def _wrong_locker_async(self):
        # Симулируем: устройство A прислало command_result с UUID команды B.
        # UPDATE ... WHERE id=X AND locker_id=A не находит строку (команда X
        # принадлежит B), fetch_one возвращает None.
        self.db_mock.fetch_one.return_value = None
        result = await self.repo.update_status_to_executed(
            _COMMAND_ID, _LOCKER_A, {"output": "ok"}
        )
        self.assertIsNone(result)

    def test_executed_with_wrong_locker_id_returns_none(self):
        asyncio.run(self._wrong_locker_async())


class TestUpdateStatusToFailedLockerScope(unittest.TestCase):
    """update_status_to_failed — симметрично: locker_id в WHERE."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.repo = CommandRepository(self.db_mock)

    async def _matching_scope_async(self):
        self.db_mock.fetch_one.return_value = _make_command_row(
            _LOCKER_A, status="failed"
        )
        result = await self.repo.update_status_to_failed(
            _COMMAND_ID, _LOCKER_A, "device error"
        )

        sql = self.db_mock.fetch_one.call_args[0][0]
        self.assertIn("AND locker_id = %s", sql)
        # Fix #3: result-колонка пишется этим же UPDATE.
        self.assertIn("result = %s::jsonb", sql)
        params = self.db_mock.fetch_one.call_args[0][1]
        # params: (error_message, result_json, command_id, locker_id)
        self.assertEqual(params[0], "device error")
        self.assertEqual(params[1], "{}")   # result по умолчанию — пустой объект
        self.assertEqual(params[2], _COMMAND_ID)
        self.assertEqual(params[3], _LOCKER_A)
        self.assertIsNotNone(result)

    def test_failed_with_matching_locker_id_updates_and_returns_command(self):
        asyncio.run(self._matching_scope_async())

    async def _with_result_payload_async(self):
        self.db_mock.fetch_one.return_value = _make_command_row(
            _LOCKER_A, status="failed"
        )
        await self.repo.update_status_to_failed(
            _COMMAND_ID, _LOCKER_A, "failed",
            result={"success": False, "message": "failed"},
        )
        params = self.db_mock.fetch_one.call_args[0][1]
        # result сериализуется в JSON и уходит вторым параметром (Fix #3).
        self.assertEqual(
            json.loads(params[1]), {"success": False, "message": "failed"}
        )

    def test_failed_writes_result_payload(self):
        asyncio.run(self._with_result_payload_async())

    async def _wrong_locker_async(self):
        self.db_mock.fetch_one.return_value = None
        result = await self.repo.update_status_to_failed(
            _COMMAND_ID, _LOCKER_A, "device error"
        )
        self.assertIsNone(result)

    def test_failed_with_wrong_locker_id_returns_none(self):
        asyncio.run(self._wrong_locker_async())


def _unique_violation(message: str = "duplicate key") -> psycopg2.errors.UniqueViolation:
    """UniqueViolation — именно тот подкласс, что psycopg2 поднимает на SQLSTATE
    23505; is_unique_violation матчит его по isinstance (pgcode у вручную
    сконструированного исключения read-only/None, его не выставить)."""
    return psycopg2.errors.UniqueViolation(message)


class TestAllocateRelayCmdIdRetryOnConflict(unittest.TestCase):
    """allocate_relay_cmd_id переживает unique-violation (конкурентный inline +
    queue-processor под READ COMMITTED оба читают одинаковый MAX и коллидируют
    на ux_locker_commands_relay_cmd_id): ловим через is_unique_violation,
    пере-вычисляем MAX+1, повторяем."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.repo = CommandRepository(self.db_mock)

    async def _one_conflict_then_success_async(self):
        # Первая попытка → UniqueViolation, вторая → успех (relay_cmd_id=8).
        self.db_mock.fetch_one.side_effect = [
            _unique_violation(),
            {"relay_cmd_id": 8},
        ]
        result = await self.repo.allocate_relay_cmd_id(_COMMAND_ID, _LOCKER_A)
        self.assertEqual(result, 8)
        # Ровно две попытки UPDATE: первая упала, вторая прошла.
        self.assertEqual(self.db_mock.fetch_one.await_count, 2)

    def test_retries_once_then_succeeds(self):
        asyncio.run(self._one_conflict_then_success_async())

    async def _non_unique_integrity_error_propagates_async(self):
        # FK/NOT-NULL violation (не UniqueViolation, SQLSTATE != 23505) НЕ
        # ретраится — пробрасываем наружу нетронутой.
        not_null = psycopg2.errors.NotNullViolation("null value")
        self.db_mock.fetch_one.side_effect = not_null
        with self.assertRaises(psycopg2.errors.NotNullViolation):
            await self.repo.allocate_relay_cmd_id(_COMMAND_ID, _LOCKER_A)
        # Один вызов: не ретраили, сразу пробросили.
        self.assertEqual(self.db_mock.fetch_one.await_count, 1)

    def test_non_unique_integrity_error_propagates(self):
        asyncio.run(self._non_unique_integrity_error_propagates_async())

    async def _no_conflict_single_call_async(self):
        self.db_mock.fetch_one.side_effect = [{"relay_cmd_id": 3}]
        result = await self.repo.allocate_relay_cmd_id(_COMMAND_ID, _LOCKER_A)
        self.assertEqual(result, 3)
        # Без коллизии — ровно один UPDATE.
        self.assertEqual(self.db_mock.fetch_one.await_count, 1)

    def test_no_conflict_single_update(self):
        asyncio.run(self._no_conflict_single_call_async())

    async def _exhausts_retries_async(self):
        # Каждая попытка коллидирует → после _RELAY_CMD_ID_MAX_RETRIES RuntimeError.
        self.db_mock.fetch_one.side_effect = _unique_violation("dup")
        with self.assertRaises(RuntimeError):
            await self.repo.allocate_relay_cmd_id(_COMMAND_ID, _LOCKER_A)

    def test_exhausts_retries_raises(self):
        asyncio.run(self._exhausts_retries_async())


class TestAckTimeoutSplitRepoMethods(unittest.TestCase):
    """COR-15: ack-timeout sweep делит зависшие 'sent' команды на два потока.

    reset_timed_out_sent_commands — у кого ОСТАЁТСЯ бюджет после инкремента
    (retry_count + 1 < max_retries) → обратно в pending, retry_count+1.
    fail_exhausted_sent_commands — у кого бюджет ИСЧЕРПАН (retry_count + 1 >=
    max_retries) → терминальный failed, RETURNING строки для публикации истории.
    Оба claim-based (CAS-guard status='sent')."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.repo = CommandRepository(self.db_mock)

    async def _reset_targets_non_exhausted_async(self):
        self.db_mock.fetch_all.return_value = [{"id": _COMMAND_ID, "type": "open"}]
        rows = await self.repo.reset_timed_out_sent_commands(5)
        self.assertEqual(len(rows), 1)
        sql = self.db_mock.fetch_all.call_args.args[0]
        # CAS-guard: только sent-строки.
        self.assertIn("status = 'sent'", sql)
        self.assertIn("status = 'pending'", sql)
        self.assertIn("retry_count = retry_count + 1", sql)
        # Только НЕ исчерпавшие бюджет (после инкремента остаётся попытка).
        self.assertIn("retry_count + 1 < max_retries", sql)
        # Только просроченные.
        self.assertIn("sent_at", sql)

    def test_reset_targets_non_exhausted(self):
        asyncio.run(self._reset_targets_non_exhausted_async())

    async def _fail_targets_exhausted_and_returns_rows_async(self):
        self.db_mock.fetch_all.return_value = [_make_command_row(_LOCKER_A, status="failed")]
        rows = await self.repo.fail_exhausted_sent_commands(5, "ack timeout")
        sql = self.db_mock.fetch_all.call_args.args[0]
        self.assertIn("status = 'failed'", sql)
        # CAS-guard: только sent-строки переводим в failed.
        self.assertIn("status = 'sent'", sql)
        # Только исчерпавшие бюджет.
        self.assertIn("retry_count + 1 >= max_retries", sql)
        # RETURNING * → список LockerCommand для публикации failed-истории.
        self.assertIn("RETURNING", sql)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].status, CommandStatus.FAILED)

    def test_fail_targets_exhausted_and_returns_rows(self):
        asyncio.run(self._fail_targets_exhausted_and_returns_rows_async())


class TestStatusGuardsAndTtl(unittest.TestCase):
    """Терминальные executed/failed не перезаписываются (CAS по активным
    статусам), а просроченные по TTL команды не выбираются и не заявляются."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock(return_value=None)
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.db_mock.execute = AsyncMock()
        self.repo = CommandRepository(self.db_mock)

    def test_active_statuses_constant(self):
        self.assertEqual(_ACTIVE_STATUSES, ["pending", "sent", "delivered"])

    def test_executed_guards_on_active_statuses(self):
        asyncio.run(self.repo.update_status_to_executed(_COMMAND_ID, _LOCKER_A, {}))
        sql, params = self.db_mock.fetch_one.call_args.args[:2]
        self.assertIn("status = ANY(%s::command_status[])", sql)
        self.assertEqual(params[3], _ACTIVE_STATUSES)

    def test_failed_guards_on_active_statuses(self):
        asyncio.run(self.repo.update_status_to_failed(_COMMAND_ID, _LOCKER_A, "x"))
        sql, params = self.db_mock.fetch_one.call_args.args[:2]
        self.assertIn("status = ANY(%s::command_status[])", sql)
        self.assertEqual(params[4], _ACTIVE_STATUSES)

    def test_get_pending_filters_by_ttl_both_branches(self):
        for locker_id in (None, _LOCKER_A):
            asyncio.run(self.repo.get_pending_commands(ttl_s=60, locker_id=locker_id))
            sql, params = self.db_mock.fetch_all.call_args.args[:2]
            self.assertIn("created_at > now() - make_interval(secs => %s)", sql)
            self.assertIn(60, params)

    def test_claim_filters_by_ttl(self):
        asyncio.run(self.repo.update_status_to_sent(_COMMAND_ID, ttl_s=60))
        sql, params = self.db_mock.fetch_one.call_args.args[:2]
        self.assertIn("created_at > now() - make_interval(secs => %s)", sql)
        self.assertEqual(params, (_COMMAND_ID, 60))

    def test_expire_is_one_idempotent_statement_with_outbox(self):
        self.db_mock.fetch_all.return_value = [_make_command_row(_LOCKER_A, status="failed")]
        rows = asyncio.run(self.repo.expire_stale_commands(
            ttl_s=60, error_message="reason", outbox_channel="command_history",
            outbox_endpoint="/x", limit=10,
        ))
        call = self.db_mock.fetch_all.call_args
        sql, params = call.args[:2]
        self.assertIn("INSERT INTO outbound_history_events", sql)
        self.assertIn("FOR UPDATE SKIP LOCKED", sql)
        self.assertEqual(call.kwargs, {"read_only": False, "idempotent": True})
        self.assertEqual(params["ttl"], 60)
        self.assertEqual(params["reason"], "reason")
        self.assertEqual(params["channel"], "command_history")
        self.assertEqual(params["endpoint"], "/x")
        self.assertEqual(params["limit"], 10)
        self.assertIsNone(params["locker_id"])
        self.assertEqual(params["active"], _ACTIVE_STATUSES)
        self.assertEqual(len(rows), 1)

    def test_release_claim_is_cas_from_sent(self):
        asyncio.run(self.repo.release_claim(_COMMAND_ID))
        call = self.db_mock.execute.call_args
        sql = call.args[0]
        self.assertIn("status = 'pending'", sql)
        self.assertIn("sent_at = NULL", sql)
        self.assertIn("AND status = 'sent'", sql)
        self.assertEqual(call.args[1], (_COMMAND_ID,))
        self.assertTrue(call.kwargs.get("idempotent"))


if __name__ == "__main__":
    unittest.main()
