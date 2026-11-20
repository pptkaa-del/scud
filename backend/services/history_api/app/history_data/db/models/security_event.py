from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from shared.enums import EventSeverity, SecurityEventType

__all__ = ["EventSeverity", "SecurityEventType", "SecurityEvent"]


@dataclass(slots=True)
class SecurityEvent:
    """
    Событие безопасности.

    Хранит информацию о нарушениях пороговых значений, взломах, отключениях и т.д.

    Attributes:
        id: ID события (BIGSERIAL)
        locker_id: UUID шкафа
        sensor_id: UUID датчика (если событие связано с датчиком)
        event_type: Тип события ('threshold_violation', 'intrusion', 'offline', 'door_tamper',
            'command_2fa_*', 'command_late_execution')
        severity: Уровень критичности
        violated_thresholds: Массив нарушенных пороговых значений (JSON)
        occurred_at: Время возникновения события
        resolved_at: Время разрешения события
        resolved_by: ID пользователя, закрывшего событие
        metadata: Дополнительные метаданные
    """
    id: int
    locker_id: str | None  # UUID; NULL для event'ов без привязки к шкафу
    sensor_id: str | None  # UUID
    event_type: SecurityEventType
    severity: EventSeverity
    violated_thresholds: list[Any]
    occurred_at: datetime
    resolved_at: datetime | None
    resolved_by: int | None
    metadata: dict[str, Any]
