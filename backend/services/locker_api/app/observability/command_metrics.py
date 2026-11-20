"""Хелперы записи метрик жизненного цикла команд (Фаза B1).

Все функции best-effort: метрика НИКОГДА не роняет доменный путь команды.
command_type — LockerCommand.type value (StrEnum CommandType → str).
"""
from datetime import datetime
import logging

from shared import metrics
from shared.enums import CommandTransition

logger = logging.getLogger(__name__)


def record_transition(command_type: str, transition: CommandTransition) -> None:
    """Увеличить счётчик переходов жизненного цикла команды.

    Best-effort: любое исключение логируется на DEBUG и игнорируется.
    """
    try:
        metrics.command_lifecycle_total.labels(
            service=metrics.SERVICE_NAME,
            command_type=command_type,
            transition=transition.value,
        ).inc()
    except Exception:
        logger.debug("record_transition failed", exc_info=True)


def record_queue_latency(
    command_type: str, created_at: datetime, sent_at: datetime | None
) -> None:
    """Зафиксировать латентность очереди (created_at → sent_at).

    Если sent_at или created_at равны None — no-op (команда ещё не отправлена).
    Best-effort: любое исключение логируется на DEBUG и игнорируется.
    """
    if sent_at is None or created_at is None:
        return
    try:
        metrics.command_queue_latency_seconds.labels(
            service=metrics.SERVICE_NAME,
            command_type=command_type,
        ).observe(max(0.0, (sent_at - created_at).total_seconds()))
    except Exception:
        logger.debug("record_queue_latency failed", exc_info=True)


def record_ack_latency(
    command_type: str, sent_at: datetime | None, end_at: datetime | None
) -> None:
    """Зафиксировать латентность подтверждения устройства (sent_at → end_at).

    Если sent_at или end_at равны None — no-op (нет обоих временных меток).
    Best-effort: любое исключение логируется на DEBUG и игнорируется.
    """
    if sent_at is None or end_at is None:
        return
    try:
        metrics.command_ack_latency_seconds.labels(
            service=metrics.SERVICE_NAME,
            command_type=command_type,
        ).observe(max(0.0, (end_at - sent_at).total_seconds()))
    except Exception:
        logger.debug("record_ack_latency failed", exc_info=True)
