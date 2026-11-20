"""TDD: SENT transition и queue latency hooks на CAS-claim точках.

Метрика queued→sent пишется в двух местах: фоновый queue-processor
(command_queue.py) и inline-доставка в CommandListHandler (command.py). Тесты
проверяют оба источника.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
import uuid

from app.observability import command_metrics
from shared import metrics
from shared.enums import CommandTransition


class _FakeAllocator:
    """Минимальный RelayCmdIdAllocator-порт для MqttTransport.prepare_send."""

    async def allocate_relay_cmd_id(self, command_id: str, locker_id: str) -> int:
        return 0xABCD

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sent_count(command_type: str) -> float:
    for m in metrics.command_lifecycle_total.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_lifecycle_total"
                and s.labels.get("command_type") == command_type
                and s.labels.get("transition") == "sent"
            ):
                return s.value
    return 0.0


def _queue_latency_count(command_type: str) -> float:
    for m in metrics.command_queue_latency_seconds.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_queue_latency_seconds_count"
                and s.labels.get("command_type") == command_type
            ):
                return s.value
    return 0.0


def _make_command(
    cmd_type: str = "reboot",
    locker_id: str | None = None,
) -> MagicMock:
    """Минимальный fake LockerCommand для unit-тестов."""
    cmd = MagicMock()
    cmd.id = str(uuid.uuid4())
    cmd.locker_id = locker_id or str(uuid.uuid4())
    cmd.type = cmd_type
    cmd.parameters = {}
    cmd.created_by = 1
    cmd.created_at = datetime.now(UTC) - timedelta(seconds=3)
    cmd.sent_at = datetime.now(UTC)
    return cmd


# ---------------------------------------------------------------------------
# Тест 1: хелпер-контракт
# ---------------------------------------------------------------------------

def test_sent_increment_and_queue_latency_on_claim() -> None:
    """Прямой вызов record_transition/record_queue_latency инкрементирует метрики.

    Этот тест проверяет контракт хелпера (B1.2), не hooks.
    """
    before_sent = _sent_count("reboot")
    before_lat = _queue_latency_count("reboot")

    created = datetime.now(UTC) - timedelta(seconds=3)
    sent = datetime.now(UTC)
    command_metrics.record_transition("reboot", CommandTransition.SENT)
    command_metrics.record_queue_latency("reboot", created, sent)

    assert _sent_count("reboot") == before_sent + 1
    assert _queue_latency_count("reboot") >= before_lat + 1


# ---------------------------------------------------------------------------
# Тест 2: command_queue.py
# ---------------------------------------------------------------------------

def test_queue_processor_records_sent_on_successful_claim() -> None:
    """start_queue_processor: при успешном CAS claim вызывает record_transition(SENT).
    """
    from app.mqtt.transport import MqttTransport
    from app.services.command_queue import CommandQueue

    locker_id = str(uuid.uuid4())
    cmd = _make_command("open", locker_id)
    cmd.relay_cmd_id = None  # аллокатор выделит id в prepare_send

    async def scenario() -> None:
        queue = CommandQueue()
        db_mock = MagicMock()
        client = MagicMock()
        client.publish = AsyncMock()
        transport = MqttTransport(locker_id, 1, client, allocator=_FakeAllocator())
        await queue.register_connection(locker_id, transport)
        with (
            patch(
                "app.locker_data.db.repositories.command.CommandRepository"
            ) as repo_cls,
            patch(
                "app.observability.command_metrics.record_transition"
            ) as mock_rec_trans,
            patch(
                "app.observability.command_metrics.record_queue_latency"
            ) as mock_rec_lat,
        ):
            repo = repo_cls.return_value
            repo.get_pending_commands = AsyncMock(return_value=[cmd])
            repo.update_status_to_sent = AsyncMock(return_value=cmd)
            task = asyncio.create_task(
                queue.start_queue_processor(db_mock, check_interval=0.05)
            )
            await asyncio.sleep(0.12)
            queue.stop_queue_processor()
            await task
        mock_rec_trans.assert_called_with(cmd.type, CommandTransition.SENT)
        mock_rec_lat.assert_called()

    asyncio.run(scenario())


def test_queue_processor_no_metric_on_lost_claim() -> None:
    """CAS промах (update_status_to_sent -> None): record_transition НЕ вызывается."""
    from app.mqtt.transport import MqttTransport
    from app.services.command_queue import CommandQueue

    locker_id = str(uuid.uuid4())
    cmd = _make_command("open", locker_id)
    cmd.relay_cmd_id = None

    async def scenario() -> None:
        queue = CommandQueue()
        db_mock = MagicMock()
        client = MagicMock()
        client.publish = AsyncMock()
        transport = MqttTransport(locker_id, 1, client, allocator=_FakeAllocator())
        await queue.register_connection(locker_id, transport)
        with (
            patch(
                "app.locker_data.db.repositories.command.CommandRepository"
            ) as repo_cls,
            patch(
                "app.observability.command_metrics.record_transition"
            ) as mock_rec_trans,
        ):
            repo = repo_cls.return_value
            repo.get_pending_commands = AsyncMock(return_value=[cmd])
            repo.update_status_to_sent = AsyncMock(return_value=None)
            task = asyncio.create_task(
                queue.start_queue_processor(db_mock, check_interval=0.05)
            )
            await asyncio.sleep(0.12)
            queue.stop_queue_processor()
            await task
        mock_rec_trans.assert_not_called()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Тест 3: command.py (inline handler)
# ---------------------------------------------------------------------------

def test_command_handler_records_sent_on_successful_inline_claim() -> None:
    """_enqueue_command: при успешном inline CAS claim вызывает record_transition(SENT)."""
    from app.handlers.command import CommandListHandler
    from app.locker_data.db.models.command import CommandType
    from app.locker_data.db.models.locker import Locker
    from app.services.command_queue import CommandQueue

    locker_id = str(uuid.uuid4())
    cmd = _make_command("open", locker_id)

    async def scenario() -> None:
        db_mock = MagicMock()
        queue_mock = MagicMock(spec=CommandQueue)
        queue_mock.is_connected.return_value = True
        queue_mock.send_command = AsyncMock(return_value=True)
        queue_mock.prepare_send = AsyncMock()
        queue_mock.command_ttl_s = 60
        locker = MagicMock(spec=Locker)
        locker.id = locker_id
        locker.inventory_number = "INV-001"
        with (
            patch("app.handlers.command.CommandRepository") as repo_cls,
            patch("app.handlers.command.CommandHistoryProducer") as hist_cls,
            patch(
                "app.observability.command_metrics.record_transition"
            ) as mock_rec_trans,
            patch(
                "app.observability.command_metrics.record_queue_latency"
            ) as mock_rec_lat,
        ):
            repo = repo_cls.return_value
            repo.create = AsyncMock(return_value=cmd)
            repo.update_status_to_sent = AsyncMock(return_value=cmd)
            hist = hist_cls.return_value
            hist.publish_created = AsyncMock()
            hist.publish_sent = AsyncMock()
            handler = object.__new__(CommandListHandler)
            handler.db = db_mock
            handler.command_queue = queue_mock
            handler._current_user_id = 1
            handler._finished = False
            def _noop_write(_data: object, status_code: int = 200) -> None:
                pass
            handler.write_json = _noop_write  # type: ignore[method-assign]
            await handler._enqueue_command(locker, CommandType.OPEN, {}, 3)
        sent_calls = [
            c for c in mock_rec_trans.call_args_list
            if c.args[1] == CommandTransition.SENT
        ]
        assert len(sent_calls) >= 1, (
            "record_transition(SENT) не был вызван после успешного inline claim"
        )
        mock_rec_lat.assert_called()

    asyncio.run(scenario())


def test_command_handler_no_metric_when_device_offline() -> None:
    """offline устройство: record_transition(SENT) не вызывается."""
    from app.handlers.command import CommandListHandler
    from app.locker_data.db.models.command import CommandType
    from app.locker_data.db.models.locker import Locker
    from app.services.command_queue import CommandQueue

    locker_id = str(uuid.uuid4())
    cmd = _make_command("open", locker_id)

    async def scenario() -> None:
        db_mock = MagicMock()
        queue_mock = MagicMock(spec=CommandQueue)
        queue_mock.is_connected.return_value = False
        locker = MagicMock(spec=Locker)
        locker.id = locker_id
        locker.inventory_number = "INV-002"
        with (
            patch("app.handlers.command.CommandRepository") as repo_cls,
            patch("app.handlers.command.CommandHistoryProducer") as hist_cls,
            patch(
                "app.observability.command_metrics.record_transition"
            ) as mock_rec_trans,
        ):
            repo = repo_cls.return_value
            repo.create = AsyncMock(return_value=cmd)
            repo.update_status_to_sent = AsyncMock(return_value=cmd)
            hist = hist_cls.return_value
            hist.publish_created = AsyncMock()
            handler = object.__new__(CommandListHandler)
            handler.db = db_mock
            handler.command_queue = queue_mock
            handler._current_user_id = 1
            handler._finished = False
            def _noop_write(_data: object, status_code: int = 200) -> None:
                pass
            handler.write_json = _noop_write  # type: ignore[method-assign]
            await handler._enqueue_command(locker, CommandType.OPEN, {}, 3)
        sent_calls = [
            c for c in mock_rec_trans.call_args_list
            if c.args[1] == CommandTransition.SENT
        ]
        assert len(sent_calls) == 0, (
            "record_transition(SENT) не должен вызываться если устройство offline"
        )

    asyncio.run(scenario())
