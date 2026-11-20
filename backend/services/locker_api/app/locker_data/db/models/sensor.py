from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class SensorDisplaySeverity(StrEnum):
    """Уровень подсветки подписи датчика в UI (не security-event severity)."""
    NORMAL = "normal"
    WARNING = "warning"
    ALERT = "alert"


@dataclass(frozen=True, slots=True)
class SensorDisplay:
    """Готовая к показу подпись перечислимого значения датчика."""
    label: str
    severity: SensorDisplaySeverity


@dataclass(slots=True)
class Sensor:
    """
    Датчик в шкафу СКУД.

    Attributes:
        id: UUID датчика
        locker_id: UUID шкафа, к которому привязан датчик
        serial_number: Серийный номер датчика
        sensor_type_id: ID типа датчика (FK к sensor_types)
        code: Код типа датчика (денормализовано из sensor_types)
        name: Название типа датчика (денормализовано из sensor_types)
        unit: Единица измерения (денормализовано из sensor_types)
        value_type: Тип значения датчика — numeric/boolean/string (денормализовано)
        current_value: Текущее значение датчика. Тип зависит от value_type:
            numeric → float, boolean → bool, string → str, no data → None.
            Хранится в БД как JSONB-обёртка {"v": value}; репозиторий
            распаковывает её при чтении и упаковывает при записи.
        created_at: Дата и время создания
        updated_at: Дата и время последнего обновления
        is_active: Активен ли датчик
        metadata: Дополнительные метаданные
        display: Подпись current_value для перечислимых датчиков (из sensor_types.states);
            None — у кода нет словаря значений.
    """
    id: str  # UUID
    locker_id: str  # UUID
    serial_number: str
    sensor_type_id: int  # FK к sensor_types
    code: str  # from sensor_types
    name: str  # from sensor_types
    unit: str | None  # from sensor_types
    value_type: str  # from sensor_types
    current_value: Any | None  # bool | float | str | None
    created_at: datetime
    updated_at: datetime
    is_active: bool
    metadata: dict[str, Any]
    display: SensorDisplay | None = None
