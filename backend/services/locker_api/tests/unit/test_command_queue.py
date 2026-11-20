"""Unit-тесты для CommandQueue"""
import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from app.locker_data.db.models.command import CommandStatus, CommandType
from app.mqtt.transport import MqttTransport
from app.services.command_expiry import CommandExpiry
from app.services.command_queue import (
    _DEFAULT_QUEUE_POLL_INTERVAL_S,
    CommandQueue,
)
from shared.enums import WsLifecycleEvent
from shared.metrics import SERVICE_NAME, command_lifecycle_total, ws_events_total


def _new_locker_id() -> str:
    return str(uuid.uuid4())


class FakeAllocator:
    """Минимальный RelayCmdIdAllocator-порт для MqttTransport.prepare_send."""

    def __init__(self, relay_cmd_id: int = 0xABCD) -> None:
        self.calls: list[tuple[str, str]] = []
        self._rid = relay_cmd_id

    async def allocate_relay_cmd_id(self, command_id: str, locker_id: str) -> int:
        self.calls.append((command_id, locker_id))
        return self._rid


class TestQueuePollIntervalDefault(unittest.TestCase):
    """Дефолтный интервал поллинга БД-очереди понижен 30→5с: устройство,
    переподключившееся между тиками, ждёт доставки команды не более 5с."""

    def test_default_poll_interval_constant(self):
        self.assertEqual(_DEFAULT_QUEUE_POLL_INTERVAL_S, 5)
        self.assertIsInstance(_DEFAULT_QUEUE_POLL_INTERVAL_S, int)

    def test_start_queue_processor_uses_default_interval(self):
        """Без явного check_interval процессор спит ровно _DEFAULT_QUEUE_POLL_INTERVAL_S."""
        async def scenario():
            queue = CommandQueue()
            db_mock = MagicMock()
            with patch('app.locker_data.db.repositories.command.CommandRepository') as repo_mock, \
                    patch('app.services.command_queue.asyncio.sleep',
                          new_callable=AsyncMock) as sleep_mock:
                repo_instance = repo_mock.return_value
                repo_instance.get_pending_commands = AsyncMock(return_value=[])

                # Останавливаем процессор после первого sleep, чтобы цикл сделал
                # ровно одну итерацию.
                async def stop_after_first(_interval):
                    queue.stop_queue_processor()

                sleep_mock.side_effect = stop_after_first

                await queue.start_queue_processor(db_mock)

            sleep_mock.assert_awaited_with(_DEFAULT_QUEUE_POLL_INTERVAL_S)

        asyncio.run(scenario())

    def test_env_override_parses_with_default_5(self):
        """main.py читает COMMAND_QUEUE_PROCESSOR_INTERVAL_S с дефолтом '5'."""
        with patch.dict(os.environ, {"COMMAND_QUEUE_PROCESSOR_INTERVAL_S": "7"}):
            self.assertEqual(
                int(os.getenv("COMMAND_QUEUE_PROCESSOR_INTERVAL_S", "5")), 7)
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                int(os.getenv("COMMAND_QUEUE_PROCESSOR_INTERVAL_S", "5")), 5)


class TestCommandQueue(unittest.TestCase):
    def setUp(self):
        self.command_queue = CommandQueue()
        self.ws_handler_mock = MagicMock()
        self.ws_handler_mock.write_message = AsyncMock()
        self.ws_handler_mock.close = MagicMock()

    def test_register_connection(self):
        """Тест: регистрация WebSocket подключения"""
        locker_id = _new_locker_id()

        asyncio.run(self.command_queue.register_connection(locker_id, self.ws_handler_mock))

        self.assertTrue(self.command_queue.is_connected(locker_id))
        self.assertEqual(self.command_queue._connections[locker_id], self.ws_handler_mock)

    def test_unregister_connection(self):
        """Тест: удаление WebSocket подключения"""
        locker_id = _new_locker_id()

        async def scenario():
            await self.command_queue.register_connection(locker_id, self.ws_handler_mock)
            self.assertTrue(self.command_queue.is_connected(locker_id))
            await self.command_queue.unregister_connection(locker_id)

        asyncio.run(scenario())
        self.assertFalse(self.command_queue.is_connected(locker_id))

    def test_unregister_only_when_handler_matches(self):
        """Старое соединение не должно стирать запись свежего соединения."""
        locker_id = _new_locker_id()
        new_handler = MagicMock()
        new_handler.write_message = AsyncMock()
        new_handler.close = MagicMock()

        async def scenario():
            await self.command_queue.register_connection(locker_id, self.ws_handler_mock)
            await self.command_queue.register_connection(locker_id, new_handler)
            # Старое соединение пытается отписаться — запись принадлежит уже другому
            await self.command_queue.unregister_connection(locker_id, self.ws_handler_mock)

        asyncio.run(scenario())

        # Старого соединения попросили закрыться при замене
        self.ws_handler_mock.close.assert_called_once()
        # Свежее соединение всё ещё живёт в реестре
        self.assertTrue(self.command_queue.is_connected(locker_id))
        self.assertIs(self.command_queue._connections[locker_id], new_handler)

    def test_register_replaces_existing_and_closes_old(self):
        """Повторная регистрация для одного locker_id вытесняет старое соединение."""
        locker_id = _new_locker_id()
        new_handler = MagicMock()
        new_handler.write_message = AsyncMock()
        new_handler.close = MagicMock()

        async def scenario():
            await self.command_queue.register_connection(locker_id, self.ws_handler_mock)
            await self.command_queue.register_connection(locker_id, new_handler)

        asyncio.run(scenario())

        self.ws_handler_mock.close.assert_called_once()
        self.assertIs(self.command_queue._connections[locker_id], new_handler)

    def test_is_connected_false(self):
        """Тест: проверка подключения несуществующего устройства"""
        locker_id = _new_locker_id()

        self.assertFalse(self.command_queue.is_connected(locker_id))

    async def _test_send_command_success_async(self):
        """Асинхронный тест: успешная отправка команды"""
        locker_id = _new_locker_id()
        command_data = {
            "type": "command",
            "command_id": str(uuid.uuid4()),
            "command_type": "open",
            "relay_cmd_id": 7,
            "parameters": {}
        }

        client = MagicMock()
        client.publish = AsyncMock()
        # Register via the real MqttTransport wrapper so send_command delegates
        # to client.publish (the transport abstraction the queue now uses).
        await self.command_queue.register_connection(
            locker_id, MqttTransport(locker_id, 1, client))

        result = await self.command_queue.send_command(locker_id, command_data)

        self.assertTrue(result)
        client.publish.assert_called_once()

    def test_send_command_success(self):
        """Тест: успешная отправка команды"""
        asyncio.run(self._test_send_command_success_async())

    async def _test_send_command_not_connected_async(self):
        """Асинхронный тест: попытка отправки команды отключенному устройству"""
        locker_id = _new_locker_id()
        command_data = {
            "type": "command",
            "command_id": str(uuid.uuid4()),
            "command_type": "open",
            "relay_cmd_id": 7,
            "parameters": {}
        }

        client = MagicMock()
        client.publish = AsyncMock()
        # Не регистрируем подключение
        result = await self.command_queue.send_command(locker_id, command_data)

        self.assertFalse(result)
        client.publish.assert_not_called()

    def test_send_command_not_connected(self):
        """Тест: попытка отправки команды отключенному устройству"""
        asyncio.run(self._test_send_command_not_connected_async())

    async def _test_queue_processor_async(self):
        """Асинхронный тест: обработка очереди команд"""
        locker_id = _new_locker_id()

        # Мокаем БД и репозиторий
        db_mock = MagicMock()
        client = MagicMock()
        client.publish = AsyncMock()

        test_command = MagicMock()
        test_command.id = str(uuid.uuid4())
        test_command.locker_id = locker_id
        test_command.type = CommandType.OPEN
        test_command.status = CommandStatus.PENDING
        test_command.parameters = {}
        test_command.relay_cmd_id = None  # аллокатор выделит id в prepare_send

        # Мокаем CommandRepository
        with patch('app.locker_data.db.repositories.command.CommandRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_pending_commands = AsyncMock(return_value=[test_command])
            repo_instance.update_status_to_sent = AsyncMock(return_value=test_command)
            repo_instance.release_claim = AsyncMock()

            # Регистрируем подключение (через MqttTransport-обёртку с аллокатором,
            # чтобы prepare_send выделил uint32 relay_cmd_id перед публикацией).
            await self.command_queue.register_connection(
                locker_id, MqttTransport(locker_id, 1, client, allocator=FakeAllocator()))

            # Запускаем обработчик очереди на одну итерацию
            task = asyncio.create_task(
                self.command_queue.start_queue_processor(db_mock, check_interval=0.1)
            )

            # Даем время на одну итерацию
            await asyncio.sleep(0.2)

            # Останавливаем обработчик
            self.command_queue.stop_queue_processor()
            await task

            # Проверяем, что команда была отправлена
            client.publish.assert_called()
            repo_instance.update_status_to_sent.assert_called_with(test_command.id, ttl_s=60)

    def test_queue_processor(self):
        """Тест: обработка очереди команд"""
        asyncio.run(self._test_queue_processor_async())

    async def _test_queue_processor_claim_before_send_async(self):
        """COR-02/18: процессор CLAIM'ит команду (pending→sent) ДО send_command.
        Если claim не удался (update_status_to_sent вернул None — другой writer
        уже забрал команду), send_command НЕ вызывается (no double-send)."""
        locker_id = _new_locker_id()
        db_mock = MagicMock()

        client = MagicMock()
        client.publish = AsyncMock()

        test_command = MagicMock()
        test_command.id = str(uuid.uuid4())
        test_command.locker_id = locker_id
        test_command.type = CommandType.OPEN
        test_command.status = CommandStatus.PENDING
        test_command.parameters = {}
        test_command.relay_cmd_id = None

        with patch('app.locker_data.db.repositories.command.CommandRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_pending_commands = AsyncMock(return_value=[test_command])
            # Claim промахнулся — команду уже забрал другой writer.
            repo_instance.update_status_to_sent = AsyncMock(return_value=None)

            await self.command_queue.register_connection(
                locker_id, MqttTransport(locker_id, 1, client, allocator=FakeAllocator()))

            task = asyncio.create_task(
                self.command_queue.start_queue_processor(db_mock, check_interval=0.1)
            )
            await asyncio.sleep(0.2)
            self.command_queue.stop_queue_processor()
            await task

            # claim был сделан, но send НЕ произошёл (claim вернул None).
            repo_instance.update_status_to_sent.assert_called_with(test_command.id, ttl_s=60)
            client.publish.assert_not_called()

    def test_queue_processor_skips_send_when_claim_lost(self):
        asyncio.run(self._test_queue_processor_claim_before_send_async())

    async def _test_send_command_error_async(self):
        """Асинхронный тест: ошибка при отправке команды"""
        locker_id = _new_locker_id()
        command_data = {
            "type": "command",
            "command_id": str(uuid.uuid4()),
            "command_type": "open",
            "relay_cmd_id": 7,
            "parameters": {},
        }

        client = MagicMock()
        # publish бросает — MqttTransport.send_command перехватывает и вернёт False.
        client.publish = AsyncMock(side_effect=Exception("Connection lost"))

        await self.command_queue.register_connection(
            locker_id, MqttTransport(locker_id, 1, client))

        result = await self.command_queue.send_command(locker_id, command_data)

        self.assertFalse(result)

    def test_send_command_error(self):
        """Тест: ошибка при отправке команды"""
        asyncio.run(self._test_send_command_error_async())


class TestAckTimeoutReset(unittest.TestCase):
    """start_ack_timeout_loop разбивает зависшие в 'sent' команды на два потока:
    с остатком retry-бюджета → обратно в 'pending' (retry_count+1, genuine
    re-attempt), исчерпавшие бюджет → терминальный 'failed' (COR-15). Оба
    перехода claim-based (CAS-guard status='sent')."""

    async def _reset_calls_both_repo_paths_async(self):
        db_mock = MagicMock()
        db_mock.execute = AsyncMock()
        repo = MagicMock()
        repo.reset_timed_out_sent_commands = AsyncMock(return_value=[])
        repo.fail_exhausted_sent_commands = AsyncMock(return_value=[])
        queue = CommandQueue()
        with patch('app.locker_data.db.repositories.command.CommandRepository',
                   return_value=repo):
            task = asyncio.create_task(
                queue.start_ack_timeout_loop(db_mock, interval_s=0.05, timeout_min=5)
            )
            await asyncio.sleep(0.1)
            queue.stop_ack_timeout_loop()
            await task

        # Не-исчерпанные сбрасываются в pending (retry_count+1).
        repo.reset_timed_out_sent_commands.assert_awaited_with(5)
        # Исчерпавшие бюджет — в терминальный failed.
        repo.fail_exhausted_sent_commands.assert_awaited()

    def test_reset_calls_both_repo_paths(self):
        asyncio.run(self._reset_calls_both_repo_paths_async())


class _FakeCommand:
    def __init__(self, cmd_id=None, locker_id="L1", cmd_type=CommandType.OPEN,
                 parameters=None, created_by=7):
        self.id = cmd_id or str(uuid.uuid4())
        self.locker_id = locker_id
        self.type = cmd_type
        self.parameters = parameters or {}
        self.created_by = created_by


class TestAckTimeoutExhaustion(unittest.TestCase):
    """COR-15: команда, исчерпавшая retry-бюджет, на ack-timeout переходит в
    терминальный 'failed' (а НЕ обратно в pending навсегда) и публикует failed
    событие истории. Команды с остатком бюджета по-прежнему уходят в pending."""

    def _build_repo(self, exhausted=None, reset_rows=None):
        repo = MagicMock()
        repo.reset_timed_out_sent_commands = AsyncMock(return_value=reset_rows or [])
        repo.fail_exhausted_sent_commands = AsyncMock(return_value=exhausted or [])
        return repo

    async def _run_one_tick(self, repo, history=None):
        db_mock = MagicMock()
        db_mock.execute = AsyncMock()
        queue = CommandQueue()
        with patch('app.locker_data.db.repositories.command.CommandRepository',
                   return_value=repo):
            task = asyncio.create_task(
                queue.start_ack_timeout_loop(
                    db_mock, interval_s=0.05, timeout_min=5,
                    history_producer=history,
                )
            )
            await asyncio.sleep(0.1)
            queue.stop_ack_timeout_loop()
            await task

    async def _exhausted_become_failed_and_publish_async(self):
        cmd = _FakeCommand()
        repo = self._build_repo(exhausted=[cmd])
        history = MagicMock()
        history.publish_failed = AsyncMock()

        await self._run_one_tick(repo, history)

        # Exhausted команды переведены в failed (claim-based bulk update).
        repo.fail_exhausted_sent_commands.assert_awaited()
        # И для каждой опубликовано failed-событие истории.
        history.publish_failed.assert_awaited()
        kwargs = history.publish_failed.await_args.kwargs
        self.assertEqual(kwargs["command_id"], cmd.id)
        self.assertEqual(kwargs["locker_id"], cmd.locker_id)
        self.assertEqual(kwargs["command_type"], cmd.type)
        self.assertIn("error_message", kwargs)

    def test_exhausted_sent_commands_become_failed_and_publish(self):
        asyncio.run(self._exhausted_become_failed_and_publish_async())

    async def _non_exhausted_reset_to_pending_async(self):
        # Нет exhausted, но есть сброшенные в pending — failed не публикуется.
        repo = self._build_repo(exhausted=[], reset_rows=[{"type": "open"}, {"type": "open"}, {"type": "open"}])
        history = MagicMock()
        history.publish_failed = AsyncMock()

        await self._run_one_tick(repo, history)

        repo.reset_timed_out_sent_commands.assert_awaited()
        history.publish_failed.assert_not_awaited()

    def test_non_exhausted_reset_to_pending_no_failed_event(self):
        asyncio.run(self._non_exhausted_reset_to_pending_async())

    async def _no_history_producer_is_tolerated_async(self):
        # history_producer=None (WS-only без mqtt) — fail всё равно происходит,
        # просто без публикации события.
        cmd = _FakeCommand()
        repo = self._build_repo(exhausted=[cmd])
        await self._run_one_tick(repo, history=None)
        repo.fail_exhausted_sent_commands.assert_awaited()

    def test_no_history_producer_is_tolerated(self):
        asyncio.run(self._no_history_producer_is_tolerated_async())


class TestPrepareSend(unittest.TestCase):
    """CommandQueue.prepare_send делегирует pre-send подготовку транспорту:
    MqttTransport с allocator-портом выделяет relay_cmd_id, без allocator —
    no-op. CommandQueue об mqtt-специфике не знает."""

    def _command(self, locker_id, relay_cmd_id=None):
        cmd = MagicMock()
        cmd.id = str(uuid.uuid4())
        cmd.locker_id = locker_id
        if relay_cmd_id is None:
            # MagicMock авто-создаёт атрибуты; явно убираем relay_cmd_id, чтобы
            # getattr(..., None) вернул None (модель LockerCommand его не несёт).
            del cmd.relay_cmd_id
        else:
            cmd.relay_cmd_id = relay_cmd_id
        return cmd

    def test_mqtt_transport_gets_relay_cmd_id_injected(self):
        async def scenario():
            queue = CommandQueue()
            alloc = FakeAllocator()
            locker_id = _new_locker_id()
            await queue.register_connection(
                locker_id, MqttTransport(locker_id, relay_id=9, client=MagicMock(),
                                         allocator=alloc))
            command = self._command(locker_id)
            data = {"type": "command"}
            await queue.prepare_send(command, data)
            self.assertEqual(data["relay_cmd_id"], 0xABCD)
            self.assertEqual(alloc.calls, [(command.id, locker_id)])

        asyncio.run(scenario())

    def test_mqtt_transport_without_allocator_does_not_allocate(self):
        """MqttTransport без allocator — prepare_send no-op (relay_cmd_id не трогаем)."""
        async def scenario():
            queue = CommandQueue()
            locker_id = _new_locker_id()
            await queue.register_connection(
                locker_id, MqttTransport(locker_id, relay_id=9, client=MagicMock()))
            command = self._command(locker_id)
            data = {"type": "command"}
            await queue.prepare_send(command, data)
            self.assertNotIn("relay_cmd_id", data)

        asyncio.run(scenario())

    def test_already_set_relay_cmd_id_is_reused_not_reallocated(self):
        async def scenario():
            queue = CommandQueue()
            alloc = FakeAllocator()
            locker_id = _new_locker_id()
            await queue.register_connection(
                locker_id, MqttTransport(locker_id, relay_id=9, client=MagicMock(),
                                         allocator=alloc))
            command = self._command(locker_id, relay_cmd_id=4242)
            data = {"type": "command"}
            await queue.prepare_send(command, data)
            # Переиспользуем существующий id, аллокатор не дёргается.
            self.assertEqual(data["relay_cmd_id"], 4242)
            self.assertEqual(alloc.calls, [])

        asyncio.run(scenario())

    def test_prepare_send_not_connected_is_noop(self):
        async def scenario():
            queue = CommandQueue()
            command = self._command(_new_locker_id())
            data = {"type": "command"}
            # Устройство не подключено — prepare_send просто ничего не делает.
            await queue.prepare_send(command, data)
            self.assertNotIn("relay_cmd_id", data)

        asyncio.run(scenario())


class TestSupersessionMetric(unittest.TestCase):
    """register_connection emits ws_events_total{event=supersession} when it
    replaces an existing connection (fires on MQTT relay reconnects too), and
    NOT on first registration. Ported from the removed WS lifecycle-metrics
    suite — the emitter now lives in CommandQueue, not the deleted WS handler."""

    @staticmethod
    def _supersession_count() -> float:
        return ws_events_total.labels(
            service=SERVICE_NAME, event=WsLifecycleEvent.SUPERSESSION.value,
        )._value.get()

    def test_supersession_increments_ws_events_total(self):
        async def scenario() -> float:
            queue = CommandQueue()
            locker_id = _new_locker_id()
            old_transport = MagicMock()
            old_transport.close = MagicMock()
            await queue.register_connection(locker_id, old_transport)
            before = self._supersession_count()
            await queue.register_connection(locker_id, MagicMock())
            return self._supersession_count() - before

        self.assertEqual(asyncio.run(scenario()), 1.0)

    def test_no_supersession_event_on_first_registration(self):
        async def scenario() -> float:
            queue = CommandQueue()
            before = self._supersession_count()
            await queue.register_connection(_new_locker_id(), MagicMock())
            return self._supersession_count() - before

        self.assertEqual(asyncio.run(scenario()), 0.0)


def _transition_count(command_type: str, transition: str) -> float:
    for m in command_lifecycle_total.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_lifecycle_total"
                and s.labels.get("command_type") == command_type
                and s.labels.get("transition") == transition
            ):
                return s.value
    return 0.0


def _pending_command(locker_id: str, command_type: CommandType = CommandType.REBOOT) -> MagicMock:
    cmd = MagicMock()
    cmd.id = str(uuid.uuid4())
    cmd.locker_id = locker_id
    cmd.type = command_type
    cmd.status = CommandStatus.PENDING
    cmd.parameters = {}
    cmd.created_by = 7
    cmd.relay_cmd_id = None
    return cmd


def _tick_queue_processor(
    repo_instance: MagicMock, locker_id: str, client: MagicMock, **kwargs,
) -> None:
    """Один тик процессора с TTL 60 с: sleep останавливает цикл."""
    queue = CommandQueue(expiry=CommandExpiry(60))

    async def scenario():
        await _register(queue, locker_id, client)
        with patch('app.locker_data.db.repositories.command.CommandRepository') as repo_mock, \
                patch('app.services.command_queue.asyncio.sleep',
                      new_callable=AsyncMock) as sleep_mock:
            repo_mock.return_value = repo_instance

            async def stop(_interval):
                queue.stop_queue_processor()

            sleep_mock.side_effect = stop
            await queue.start_queue_processor(MagicMock(), **kwargs)

    asyncio.run(scenario())


def _repo(commands: list) -> MagicMock:
    repo = MagicMock()
    repo.expire_stale_commands = AsyncMock(return_value=[])
    repo.get_pending_commands = AsyncMock(return_value=commands)
    repo.update_status_to_sent = AsyncMock(side_effect=lambda cid, ttl_s: commands[0])
    repo.release_claim = AsyncMock()
    return repo


async def _register(queue: CommandQueue, locker_id: str, client: MagicMock) -> None:
    await queue.register_connection(
        locker_id, MqttTransport(locker_id, 1, client, allocator=FakeAllocator()))


class TestQueueTtlAndDispatch(unittest.TestCase):

    def test_queue_processor_publishes_sent_after_dispatch(self):
        locker_id = _new_locker_id()
        client = MagicMock()
        client.publish = AsyncMock()
        cmd = _pending_command(locker_id)
        history = MagicMock()
        history.publish_sent = AsyncMock()
        _tick_queue_processor(_repo([cmd]), locker_id, client,
                      history_producer=history)
        client.publish.assert_awaited()
        history.publish_sent.assert_awaited_once()
        self.assertEqual(history.publish_sent.await_args.kwargs["command_id"], cmd.id)

    def test_send_false_releases_claim_no_sent_event_no_sent_metric(self):
        locker_id = _new_locker_id()
        client = MagicMock()
        client.publish = AsyncMock(side_effect=Exception("broker down"))
        cmd = _pending_command(locker_id, CommandType.CLOSE)
        repo = _repo([cmd])
        history = MagicMock()
        history.publish_sent = AsyncMock()
        sent_before = _transition_count("close", "sent")
        failed_before = _transition_count("close", "send_failed")

        _tick_queue_processor(repo, locker_id, client, history_producer=history)

        repo.release_claim.assert_awaited_once_with(cmd.id)
        history.publish_sent.assert_not_awaited()
        client.publish.assert_awaited()
        self.assertEqual(_transition_count("close", "send_failed") - failed_before, 1)
        self.assertEqual(_transition_count("close", "sent"), sent_before)

    def test_tick_sweeps_then_fetches_with_ttl(self):
        locker_id = _new_locker_id()
        client = MagicMock()
        client.publish = AsyncMock()
        cmd = _pending_command(locker_id)
        repo = _repo([cmd])
        order: list[str] = []
        repo.expire_stale_commands.side_effect = lambda **_kw: order.append("expire") or []
        repo.get_pending_commands.side_effect = lambda **_kw: order.append("fetch") or [cmd]

        _tick_queue_processor(repo, locker_id, client)

        self.assertEqual(order, ["expire", "fetch"])
        repo.get_pending_commands.assert_awaited_once_with(ttl_s=60)
        repo.update_status_to_sent.assert_awaited_once_with(cmd.id, ttl_s=60)

    def test_sweep_error_does_not_block_delivery(self):
        locker_id = _new_locker_id()
        client = MagicMock()
        client.publish = AsyncMock()
        cmd = _pending_command(locker_id)
        repo = _repo([cmd])
        repo.expire_stale_commands.side_effect = RuntimeError("db hiccup")

        _tick_queue_processor(repo, locker_id, client)

        repo.update_status_to_sent.assert_awaited_once()
        client.publish.assert_awaited()

    def test_command_ttl_default_without_expiry(self):
        self.assertEqual(CommandQueue().command_ttl_s, 60)
        self.assertEqual(CommandQueue(expiry=CommandExpiry(15)).command_ttl_s, 15)


if __name__ == '__main__':
    unittest.main()
