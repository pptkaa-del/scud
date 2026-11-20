"""
DTO для Security Events API.

Соответствует OpenAPI схемам:
- SecurityEventCreate (openapi/schemas/security_event.yaml#/SecurityEventCreate)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...history_data.db.models.security_event import EventSeverity, SecurityEventType


@dataclass
class SecurityEventCreateDTO:
    """
    DTO для создания события безопасности.

    Соответствует OpenAPI схеме SecurityEventCreate.

    Required fields:
        locker_id: UUID шкафа
        event_type: Тип события (автоматически конвертируется из строки в SecurityEventType enum)
        severity: Уровень критичности (автоматически конвертируется из строки в EventSeverity enum)

    Optional fields:
        sensor_id: UUID датчика (если событие связано с датчиком)
        violated_thresholds: Массив нарушенных пороговых значений
        metadata: Дополнительные метаданные

    Note:
        При использовании с dacite.Config(cast=[EventSeverity, SecurityEventType]),
        строки автоматически конвертируются в enum и валидируются.
    """
    event_type: SecurityEventType  # Автоматическая конвертация и валидация
    severity: EventSeverity         # Автоматическая конвертация и валидация
    # NULL допустим для аудит-событий вне домена конкретного шкафа
    # (например, command_2fa_failed при ненайденном challenge_id).
    locker_id: str | None = None  # UUID
    sensor_id: str | None = None  # UUID
    violated_thresholds: list[Any] | None = field(default_factory=list)
    metadata: dict[str, Any] | None = field(default_factory=dict)
    # ARC-02: продьюсер-генерируемый UUID для идемпотентности повторной
    # доставки (at-least-once). NULL допустим для обратной совместимости —
    # такие события вставляются без дедупликации.
    event_uid: str | None = None  # UUID
