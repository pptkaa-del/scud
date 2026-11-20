"""Состояния перечислимых датчиков из sensor_types.states: валидация и подписи."""
from __future__ import annotations

import json
import logging
from typing import Final

from .locker_data.db.models.sensor import SensorDisplay, SensorDisplaySeverity
from .locker_data.db.models.sensor_type import SensorStateOption, SensorValueType

logger = logging.getLogger(__name__)

MAX_STATES: Final = 32
MAX_LABEL_LENGTH: Final = 100
NO_DATA_LABEL: Final = "Нет данных"
UNKNOWN_LABEL: Final = "Неизвестное значение"
UNKNOWN_LABEL_WITH_RAW: Final = "Неизвестное значение ({raw})"
ENUM_RANGE_THRESHOLD_ERROR: Final = (
    "min_value/max_value неприменимы к датчику с перечислимыми значениями"
)
_STATE_KEYS: Final = frozenset({"value", "label", "severity"})
_FIXED_VALUE_ERROR: Final = "fixed_value должен быть одним из: {allowed}"
_BOOL_KEYS: Final = {True: "true", False: "false"}


def _parse_value(raw: object, value_type: str) -> int | bool:
    if value_type == SensorValueType.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        raise ValueError("states[].value boolean-датчика должен быть true/false")
    if isinstance(raw, int) and not isinstance(raw, bool):
        return raw
    raise ValueError("states[].value числового датчика должен быть целым числом")


def _parse_option(raw: object, value_type: str) -> SensorStateOption:
    if not isinstance(raw, dict) or set(raw) != _STATE_KEYS:
        raise ValueError("элемент states должен содержать ровно value, label, severity")
    label = raw["label"]
    if not isinstance(label, str) or not label.strip() or len(label.strip()) > MAX_LABEL_LENGTH:
        raise ValueError(f"states[].label — непустая строка до {MAX_LABEL_LENGTH} символов")
    try:
        severity = SensorDisplaySeverity(raw["severity"])
    except ValueError as exc:
        allowed = ", ".join(s.value for s in SensorDisplaySeverity)
        raise ValueError(f"states[].severity должен быть одним из: {allowed}") from exc
    return SensorStateOption(_parse_value(raw["value"], value_type), label.strip(), severity)


def parse_states(raw: object, value_type: str) -> list[SensorStateOption]:
    if value_type == SensorValueType.STRING:
        raise ValueError("states неприменимы к датчику с value_type=string")
    if not isinstance(raw, list) or not raw or len(raw) > MAX_STATES:
        raise ValueError(f"states — непустой массив до {MAX_STATES} элементов")
    options = [_parse_option(item, value_type) for item in raw]
    keys = [int(option.value) for option in options]
    if len(set(keys)) != len(keys):
        raise ValueError("значения states[].value должны быть уникальны")
    return sorted(options, key=lambda option: int(option.value))


def states_from_db(raw: object, value_type: str) -> list[SensorStateOption] | None:
    if raw is None:
        return None
    try:
        return parse_states(raw, value_type)
    except ValueError:
        logger.warning("sensor_types.states is corrupt, display disabled: %r", raw)
        return None


def states_to_json(states: list[SensorStateOption]) -> str:
    return json.dumps(
        [{"value": s.value, "label": s.label, "severity": s.severity.value} for s in states],
        ensure_ascii=False,
    )


def states_to_raw(states: list[SensorStateOption]) -> list[dict[str, object]]:
    raw: list[dict[str, object]] = json.loads(states_to_json(states))
    return raw


def _state_key(value: object) -> int | None:
    if isinstance(value, bool | int):
        return int(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return None


def resolve_display(states: list[SensorStateOption] | None, value: object) -> SensorDisplay | None:
    if states is None:
        return None
    if value is None:
        return SensorDisplay(NO_DATA_LABEL, SensorDisplaySeverity.NORMAL)
    key = _state_key(value)
    if key is None:
        return SensorDisplay(UNKNOWN_LABEL, SensorDisplaySeverity.WARNING)
    for option in states:
        if int(option.value) == key:
            return SensorDisplay(option.label, option.severity)
    return SensorDisplay(UNKNOWN_LABEL_WITH_RAW.format(raw=key), SensorDisplaySeverity.WARNING)


def fixed_value_key(value: int | bool, value_type: str) -> str:
    if value_type == SensorValueType.BOOLEAN:
        return _BOOL_KEYS[bool(value)]
    return str(int(value))


def int_like(raw: object) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, str):
        try:
            number = float(raw.strip())
        except ValueError:
            return None
        return int(number) if number.is_integer() else None
    return _state_key(raw)


def canonical_fixed_value(states: list[SensorStateOption], value_type: str, raw: object) -> str:
    allowed = sorted(int(s.value) for s in states)
    key = int_like(raw)
    if key is None or key not in allowed:
        raise ValueError(_FIXED_VALUE_ERROR.format(allowed=", ".join(map(str, allowed))))
    return fixed_value_key(key, value_type)
