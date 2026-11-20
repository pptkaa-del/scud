"""
DTO для Telemetry API.

Соответствует OpenAPI схемам:
- TelemetryReading (openapi/schemas/telemetry.yaml:47)
- TelemetryBatchRequest (openapi/schemas/telemetry.yaml:81)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class TelemetryReadingDTO:
    """
    DTO для одной записи телеметрии.

    Соответствует OpenAPI схеме TelemetryReading.

    Required fields:
        sensor_id: UUID датчика
        sensor_type_code: Код типа датчика (temperature, humidity, door, etc.)

    Optional fields:
        locker_id: UUID шкафа (если не указан в root TelemetryBatchRequest)
        value: Числовое значение датчика
        value_text: Текстовое/булевое значение (для не-числовых типов)
        recorded_at: Временная метка записи (по умолчанию - текущее время)
        metadata: Дополнительные метаданные
    """
    sensor_id: str  # UUID
    sensor_type_code: str
    locker_id: str | None = None  # UUID
    value: float | None = None
    value_text: str | None = None
    recorded_at: datetime | None = None
    metadata: dict[str, Any] | None = field(default_factory=dict)


@dataclass
class TelemetryBatchRequestDTO:
    """
    DTO для batch-запроса телеметрии.

    Соответствует OpenAPI схеме TelemetryBatchRequest.

    Required fields:
        readings: Массив записей телеметрии (1-1000 записей)

    Optional fields:
        locker_id: UUID шкафа (применяется ко всем readings, если не указан в них)

    Note:
        readings хранятся как list[dict] для гибкости batch операций.
        Конвертация в TelemetryReadingDTO выполняется при необходимости.
    """
    readings: list[dict[str, Any]]  # Массив TelemetryReading (как dict для гибкости)
    locker_id: str | None = None  # UUID
