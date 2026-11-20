"""TDD: EXECUTED/FAILED transition hooks + ack latency on the MQTT relay ack path.

Шаг 1 (RED): тесты описывают желаемый контракт.
Шаг 3 (GREEN): тесты проходят после добавления вызовов в mqtt/results.py.

Уникальные command_type per test: OPEN (executed-counter), CLOSE (failed-counter),
CONFIG (executed-latency), REBOOT (failed-latency) — избегаем пересечения счётчиков
с WS-тестами (open/close/reboot) при совместном запуске pytest-сессии.
"""
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from app.mqtt.relay_protocol import RepResult
from app.mqtt.results import RelayCommandResults
from shared import metrics
from shared.enums import CommandStatus, CommandType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _transition_count(command_type: str, transition: str) -> float:
    for m in metrics.command_lifecycle_total.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_lifecycle_total"
                and s.labels.get("command_type") == command_type
                and s.labels.get("transition") == transition
            ):
                return s.value
    return 0.0


def _ack_latency_count(command_type: str) -> float:
    for m in metrics.command_ack_latency_seconds.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_ack_latency_seconds_count"
                and s.labels.get("command_type") == command_type
            ):
                return s.value
    return 0.0


def _make_handler() -> RelayCommandResults:
    handler = RelayCommandResults.__new__(RelayCommandResults)
    handler._commands = MagicMock()
    handler._commands.get_by_id = AsyncMock(return_value=None)
    handler._history = MagicMock(
        publish_executed=AsyncMock(),
        publish_failed=AsyncMock(),
    )
    handler._applier = None
    handler._locker_repo = None
    handler._notifier = None
    return handler


def _make_cmd(
    cmd_id: str,
    cmd_type: str,
    *,
    executed_at: datetime | None,
    failed_at: datetime | None,
) -> SimpleNamespace:
    now = datetime.now(UTC)
    return SimpleNamespace(
        id=cmd_id, locker_id="l1", type=cmd_type,
        status=CommandStatus.EXECUTED if executed_at else CommandStatus.FAILED,
        parameters={}, created_at=now - timedelta(seconds=5),
        created_by=1, sent_at=now - timedelta(seconds=2), delivered_at=None,
        executed_at=executed_at, failed_at=failed_at, result={}, error_message=None,
        retry_count=0, max_retries=3, relay_cmd_id=5,
    )


# ---------------------------------------------------------------------------
# Test 1: успешный rep → EXECUTED + ack latency  (command_type: open, config)
# ---------------------------------------------------------------------------

def test_mqtt_executed_increments_metric() -> None:
    """MQTT SUCCESS rep: счётчик transition=executed растёт на 1."""
    now = datetime.now(UTC)
    cmd = _make_cmd("c1", CommandType.OPEN.value, executed_at=now, failed_at=None)
    handler = _make_handler()
    handler._commands.resolve_relay_cmd_id = AsyncMock(return_value="c1")
    handler._commands.update_status_to_executed = AsyncMock(return_value=cmd)

    before = _transition_count("open", "executed")
    asyncio.run(handler.handle_rep("l1", 5, RepResult.SUCCESS))
    assert _transition_count("open", "executed") == before + 1


def test_mqtt_executed_records_ack_latency() -> None:
    """MQTT SUCCESS rep: histogram ack_latency_seconds получает наблюдение.

    Использует command_type=config, чтобы не пересекаться с другими тестами.
    """
    now = datetime.now(UTC)
    cmd = _make_cmd("c3", CommandType.CONFIG.value, executed_at=now, failed_at=None)
    handler = _make_handler()
    handler._commands.resolve_relay_cmd_id = AsyncMock(return_value="c3")
    handler._commands.update_status_to_executed = AsyncMock(return_value=cmd)

    before_lat = _ack_latency_count("config")
    asyncio.run(handler.handle_rep("l1", 5, RepResult.SUCCESS))
    assert _ack_latency_count("config") == before_lat + 1


# ---------------------------------------------------------------------------
# Test 2: failed rep → FAILED + ack latency  (command_type: close, reboot)
# ---------------------------------------------------------------------------

def test_mqtt_failed_increments_metric() -> None:
    """MQTT FAILURE rep: счётчик transition=failed растёт на 1."""
    now = datetime.now(UTC)
    cmd = _make_cmd("c2", CommandType.CLOSE.value, executed_at=None, failed_at=now)
    handler = _make_handler()
    handler._commands.resolve_relay_cmd_id = AsyncMock(return_value="c2")
    handler._commands.update_status_to_failed = AsyncMock(return_value=cmd)

    before = _transition_count("close", "failed")
    asyncio.run(handler.handle_rep("l1", 7, RepResult.FAILURE))
    assert _transition_count("close", "failed") == before + 1


def test_mqtt_failed_records_ack_latency() -> None:
    """MQTT FAILURE rep: histogram ack_latency_seconds получает наблюдение.

    Использует command_type=reboot, чтобы не пересекаться с другими тестами.
    """
    now = datetime.now(UTC)
    cmd = _make_cmd("c4", CommandType.REBOOT.value, executed_at=None, failed_at=now)
    handler = _make_handler()
    handler._commands.resolve_relay_cmd_id = AsyncMock(return_value="c4")
    handler._commands.update_status_to_failed = AsyncMock(return_value=cmd)

    before_lat = _ack_latency_count("reboot")
    asyncio.run(handler.handle_rep("l1", 7, RepResult.FAILURE))
    assert _ack_latency_count("reboot") == before_lat + 1


# ---------------------------------------------------------------------------
# Test 3: None guard — resolve_relay_cmd_id вернул None → ни один счётчик не
# двигается (неизвестный relay_cmd_id)
# ---------------------------------------------------------------------------

def test_mqtt_unknown_relay_cmd_does_not_increment() -> None:
    """resolve_relay_cmd_id → None: ни EXECUTED, ни FAILED не инкрементируются."""
    handler = _make_handler()
    handler._commands.resolve_relay_cmd_id = AsyncMock(return_value=None)

    before_exec = _transition_count("open", "executed")
    before_fail = _transition_count("close", "failed")
    asyncio.run(handler.handle_rep("l1", 99, RepResult.SUCCESS))
    assert _transition_count("open", "executed") == before_exec
    assert _transition_count("close", "failed") == before_fail


# ---------------------------------------------------------------------------
# Test 4: cross-locker guard — update_status_to_* вернул None → счётчики не
# двигаются
# ---------------------------------------------------------------------------

def test_mqtt_mismatched_executed_does_not_increment() -> None:
    """update_status_to_executed → None (cross-locker): EXECUTED не инкрементируется."""
    handler = _make_handler()
    handler._commands.resolve_relay_cmd_id = AsyncMock(return_value="c_foreign")
    handler._commands.update_status_to_executed = AsyncMock(return_value=None)

    before = _transition_count("open", "executed")
    asyncio.run(handler.handle_rep("l1", 5, RepResult.SUCCESS))
    assert _transition_count("open", "executed") == before


def test_mqtt_mismatched_failed_does_not_increment() -> None:
    """update_status_to_failed → None (cross-locker): FAILED не инкрементируется."""
    handler = _make_handler()
    handler._commands.resolve_relay_cmd_id = AsyncMock(return_value="c_foreign")
    handler._commands.update_status_to_failed = AsyncMock(return_value=None)

    before = _transition_count("close", "failed")
    asyncio.run(handler.handle_rep("l1", 7, RepResult.FAILURE))
    assert _transition_count("close", "failed") == before
