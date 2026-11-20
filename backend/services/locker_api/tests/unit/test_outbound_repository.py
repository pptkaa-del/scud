"""Unit-тесты OutboundRepository.fetch_pending — атомарный claim+lease.

fetch_pending заявляет недоставленные строки через `UPDATE ... RETURNING` с
`FOR UPDATE SKIP LOCKED` и сдвигом `next_attempt_at` в будущее (lease). Эти
тесты с моком db.fetch_all проверяют форму SQL и порядок биндов; реальную
конкурентность (SKIP LOCKED не отдаёт строку двум репликам) ловят
integration-тесты.
"""

import asyncio
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.locker_data.db.repositories.outbound import (
    _CLAIM_LEASE_SECONDS,
    OutboundEvent,
    OutboundRepository,
)


def _make_row(event_id: int = 7) -> dict:
    """Минимальный row, который fetch_pending маппит в OutboundEvent."""
    return {
        "id": event_id,
        "channel": "telemetry",
        "endpoint": "/internal/api/v1/telemetry",
        "payload": {"readings": []},
        "created_at": datetime.now(),
        "attempt_count": 0,
        "next_attempt_at": datetime.now(),
        "last_error": None,
        "delivered_at": None,
        "locker_id": None,
        "dead_lettered_at": None,
    }


class TestFetchPendingClaim(unittest.TestCase):
    """fetch_pending заявляет строки через UPDATE ... RETURNING + SKIP LOCKED."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[_make_row()])
        self.repo = OutboundRepository(self.db_mock)

    async def _default_lease_async(self):
        await self.repo.fetch_pending(limit=50)
        sql = self.db_mock.fetch_all.call_args.args[0]
        params = self.db_mock.fetch_all.call_args.args[1]
        return sql, params

    def test_uses_update_returning_with_skip_locked(self):
        sql, _ = asyncio.run(self._default_lease_async())
        upper = sql.upper()
        # Атомарный claim+lease обёрнут в CTE.
        self.assertIn("WITH", upper)
        self.assertIn("UPDATE OUTBOUND_HISTORY_EVENTS", upper)
        self.assertIn("RETURNING", upper)
        # Конкурентные реплики не хватают одни и те же строки.
        self.assertIn("FOR UPDATE SKIP LOCKED", upper)
        # Фильтр недоставленных и готовых к попытке.
        self.assertIn("DELIVERED_AT IS NULL", upper)
        self.assertIn("NEXT_ATTEMPT_AT <= NOW()", upper)
        # FIFO детерминирован внешним ORDER BY id поверх CTE: RETURNING сам
        # порядок не сохраняет, поэтому сортировка должна быть в хвосте SQL.
        self.assertIn("ORDER BY ID", upper)
        self.assertTrue(
            upper.rstrip().endswith("ORDER BY ID"),
            "Внешний ORDER BY id должен завершать SQL — иначе FIFO недетерминирован.",
        )

    def test_excludes_dead_lettered_and_budgets_bytes(self):
        """SQL пропускает запаркованные строки и ограничивает батч по байтам."""
        sql, _ = asyncio.run(self._default_lease_async())
        upper = sql.upper()
        # Dead-letter строки не выбираются (хвост очереди не растёт безгранично).
        self.assertIn("DEAD_LETTERED_AT IS NULL", upper)
        # Бюджет по байтам: нарастающий pg_column_size с COALESCE-обходом NULL.
        self.assertIn("PG_COLUMN_SIZE(PAYLOAD)", upper)
        self.assertIn("COALESCE", upper)
        # locker_id и dead_lettered_at возвращаются для маппинга в OutboundEvent.
        self.assertIn("LOCKER_ID", upper)

    def test_binds_limit_bytes_lease_in_order(self):
        _, params = asyncio.run(self._default_lease_async())
        # Порядок биндов: (limit, max_bytes, lease_seconds).
        self.assertEqual(params[0], 50)
        self.assertIsNone(params[1])  # max_bytes по умолчанию None → бюджет off
        self.assertEqual(params[2], _CLAIM_LEASE_SECONDS)

    def test_default_lease_constant_applied_when_omitted(self):
        async def _run():
            await self.repo.fetch_pending(limit=10)
            return self.db_mock.fetch_all.call_args.args[1]

        params = asyncio.run(_run())
        self.assertEqual(params[2], _CLAIM_LEASE_SECONDS)

    def test_explicit_lease_seconds_override(self):
        async def _run():
            await self.repo.fetch_pending(limit=10, claim_lease_seconds=42.0)
            return self.db_mock.fetch_all.call_args.args[1]

        params = asyncio.run(_run())
        self.assertEqual(params[2], 42.0)

    def test_max_bytes_bound_passed_through(self):
        async def _run():
            await self.repo.fetch_pending(limit=10, max_bytes=1024)
            return self.db_mock.fetch_all.call_args.args[1]

        params = asyncio.run(_run())
        self.assertEqual(params[1], 1024)

    def test_runs_as_write_transaction(self):
        # UPDATE ... RETURNING нельзя гонять в READ ONLY транзакции.
        asyncio.run(self.repo.fetch_pending(limit=5))
        kwargs = self.db_mock.fetch_all.call_args.kwargs
        self.assertFalse(kwargs.get("read_only", True))

    def test_row_mapping_unchanged(self):
        # Маппинг row → OutboundEvent остаётся прежним.
        events = asyncio.run(self.repo.fetch_pending(limit=5))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertIsInstance(event, OutboundEvent)
        self.assertEqual(event.id, 7)
        self.assertEqual(event.channel, "telemetry")
        self.assertEqual(event.endpoint, "/internal/api/v1/telemetry")
        self.assertEqual(event.payload, {"readings": []})
        self.assertEqual(event.attempt_count, 0)
        self.assertIsNone(event.last_error)
        self.assertIsNone(event.delivered_at)

    def test_payload_json_string_is_decoded(self):
        # payload приходит строкой (не dict) → json.loads, как раньше.
        async def _run():
            row = _make_row()
            row["payload"] = '{"readings": [1, 2]}'
            self.db_mock.fetch_all = AsyncMock(return_value=[row])
            return await self.repo.fetch_pending(limit=5)

        events = asyncio.run(_run())
        self.assertEqual(events[0].payload, {"readings": [1, 2]})


class TestEnqueueLockerLink(unittest.TestCase):
    """enqueue проставляет locker_id из payload для FK-привязки (cascade-очистка)."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one_strict = AsyncMock(return_value={"id": 1})
        self.repo = OutboundRepository(self.db_mock)

    def _enqueue_params(self, payload: dict, **kw) -> tuple:
        async def _run():
            await self.repo.enqueue(
                "telemetry", "/internal/api/v1/telemetry", payload, **kw,
            )
            return self.db_mock.fetch_one_strict.call_args.args[1]

        return asyncio.run(_run())

    def test_extracts_top_level_locker_id(self):
        lid = "11111111-1111-1111-1111-111111111111"
        self.assertIn(lid, self._enqueue_params({"locker_id": lid, "readings": []}))

    def test_extracts_form_fields_locker_id_for_media(self):
        lid = "22222222-2222-2222-2222-222222222222"
        params = self._enqueue_params(
            {"form_fields": {"locker_id": lid}, "file_bytes_b64": "x"},
        )
        self.assertIn(lid, params)

    def test_non_uuid_locker_id_stored_as_none(self):
        # Нестандартный payload (тестовое 'l') не должен ронять FK-вставку.
        params = self._enqueue_params({"locker_id": "l", "readings": []})
        self.assertIsNone(params[3])

    def test_explicit_locker_id_wins_over_payload(self):
        explicit = "33333333-3333-3333-3333-333333333333"
        params = self._enqueue_params(
            {"locker_id": "44444444-4444-4444-4444-444444444444"},
            locker_id=explicit,
        )
        self.assertEqual(params[3], explicit)


class TestMarkDeadLettered(unittest.TestCase):
    """mark_dead_lettered паркует событие и инкрементирует attempt_count."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.execute = AsyncMock()
        self.repo = OutboundRepository(self.db_mock)

    def test_sets_dead_lettered_at_and_increments(self):
        asyncio.run(self.repo.mark_dead_lettered(7, "boom"))
        sql = self.db_mock.execute.call_args.args[0].upper()
        params = self.db_mock.execute.call_args.args[1]
        self.assertIn("DEAD_LETTERED_AT = NOW()", sql)
        self.assertIn("ATTEMPT_COUNT = ATTEMPT_COUNT + 1", sql)
        self.assertEqual(params[-1], 7)  # event_id — последний бинд


if __name__ == "__main__":
    unittest.main()
