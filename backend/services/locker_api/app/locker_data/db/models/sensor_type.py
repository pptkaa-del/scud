"""
Модель типа датчика (SensorType).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from .sensor import SensorDisplaySeverity


class SensorValueType(StrEnum):
    """Тип значения датчика."""
    NUMERIC = "numeric"
    BOOLEAN = "boolean"
    STRING = "string"


class SensorCode(StrEnum):
    """Коды типов датчиков, на которые опирается код locker_api."""
    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    SHOCK = "shock"
    POWER = "power"
    INTRUSION = "intrusion"
    DOOR = "door"
    TAMPER = "tamper"
    BATTERY = "battery"
    SIGNAL = "signal"
    NET_TYPE = "net_type"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class SensorStateOption:
    """Одно допустимое значение перечислимого датчика и его подпись."""
    value: int | bool
    label: str
    severity: SensorDisplaySeverity


@dataclass(slots=True)
class SensorType:
    """
    Тип датчика в системе СКУД.

    Attributes:
        id: Уникальный идентификатор типа
        code: Код типа датчика (temperature, humidity, etc.)
        name: Человекочитаемое название
        description: Описание типа датчика
        unit: Единица измерения (°C, %, V, и т.д.)
        value_type: Тип значения (numeric, boolean, string)
        created_at: Дата и время создания записи
        states: Словарь значений для перечислимых типов, иначе None
    """
    id: int
    code: str
    name: str
    description: str | None
    unit: str | None
    value_type: SensorValueType
    created_at: datetime
    states: list[SensorStateOption] | None = None
