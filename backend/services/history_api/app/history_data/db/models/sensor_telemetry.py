from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(slots=True)
class SensorTelemetry:
    """
    Запись телеметрии датчика.

    Хранится в партиционированной таблице sensor_telemetry.
    Партиционирование по месяцам для оптимизации работы с большими объемами данных.

    Attributes:
        id: ID записи (BIGSERIAL)
        locker_id: UUID шкафа
        sensor_id: UUID датчика
        sensor_type_code: Код типа датчика (денормализация)
        value: Числовое значение датчика
        value_text: Текстовое значение датчика (для строковых/булевых)
        recorded_at: Временная метка записи
        metadata: Дополнительные метаданные (JSONB)
    """
    id: int
    locker_id: str  # UUID
    sensor_id: str  # UUID
    sensor_type_code: str
    value: float | None
    value_text: str | None
    recorded_at: datetime
    metadata: dict[str, Any]
