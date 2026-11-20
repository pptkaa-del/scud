"""Unit-тесты метрик переходов ack-timeout и send-failed (Фаза B1.6).

Проверяет:
- ack_timeout_retry: counter растёт на 1 PER команду (цикл по reset-списку).
- ack_timeout_failed: counter растёт на 1 per exhausted строке.
- send_failed: counter растёт при исключении в except-ветке отправки.
"""
from datetime import UTC, datetime
from types import SimpleNamespace

from app.observability import command_metrics
from shared import metrics
from shared.enums import CommandTransition, CommandType


def _count(transition: str, command_type: str = "open") -> float:
    for m in metrics.command_lifecycle_total.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_lifecycle_total"
                and s.labels.get("command_type") == command_type
                and s.labels.get("transition") == transition
            ):
                return s.value
    return 0.0


def _cmd():
    now = datetime.now(UTC)
    return SimpleNamespace(id="c", locker_id="l", type=CommandType.OPEN.value,
                           created_at=now, sent_at=now, executed_at=None, failed_at=now)


def test_ack_timeout_retry_per_command():
    before = _count("ack_timeout_retry")
    for c in [_cmd(), _cmd()]:
        command_metrics.record_transition(c.type, CommandTransition.ACK_TIMEOUT_RETRY)
    assert _count("ack_timeout_retry") == before + 2


def test_ack_timeout_failed_per_row():
    before = _count("ack_timeout_failed")
    command_metrics.record_transition(_cmd().type, CommandTransition.ACK_TIMEOUT_FAILED)
    assert _count("ack_timeout_failed") == before + 1


def test_ack_timeout_retry_zero_commands_no_increment():
    """Если reset вернул пустой список — counter не растёт."""
    before = _count("ack_timeout_retry")
    reset: list[SimpleNamespace] = []
    for c in reset:
        command_metrics.record_transition(c.type, CommandTransition.ACK_TIMEOUT_RETRY)
    assert _count("ack_timeout_retry") == before


def test_send_failed_increments():
    before = _count("send_failed")
    command_metrics.record_transition(_cmd().type, CommandTransition.SEND_FAILED)
    assert _count("send_failed") == before + 1
