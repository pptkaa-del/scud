"""Парсинг, валидация и вычисление подписей состояний перечислимых датчиков."""
import json

import pytest

from app.locker_data.db.models.sensor import SensorDisplay, SensorDisplaySeverity
from app.locker_data.db.models.sensor_type import SensorStateOption
from app.sensor_states import (
    MAX_STATES,
    NO_DATA_LABEL,
    canonical_fixed_value,
    fixed_value_key,
    parse_states,
    resolve_display,
    states_from_db,
    states_to_json,
    states_to_raw,
)

NORMAL = SensorDisplaySeverity.NORMAL
WARNING = SensorDisplaySeverity.WARNING
ALERT = SensorDisplaySeverity.ALERT

POWER = [
    {"value": 0, "label": "От батареи", "severity": "warning"},
    {"value": 1, "label": "От сети", "severity": "normal"},
]
ERROR = [
    {"value": False, "label": "Норма", "severity": "normal"},
    {"value": True, "label": "Ошибка", "severity": "alert"},
]


def test_parse_numeric_states():
    assert parse_states(POWER, "numeric") == [
        SensorStateOption(0, "От батареи", WARNING),
        SensorStateOption(1, "От сети", NORMAL),
    ]


def test_parse_sorts_options_by_value():
    parsed = parse_states(list(reversed(POWER)), "numeric")
    assert [int(o.value) for o in parsed] == [0, 1]
    parsed = parse_states(list(reversed(ERROR)), "boolean")
    assert [o.value for o in parsed] == [False, True]


def test_parse_strips_label():
    raw = [{"value": 1, "label": "  От сети ", "severity": "normal"}]
    assert parse_states(raw, "numeric")[0].label == "От сети"


@pytest.mark.parametrize(("raw", "value_type"), [
    ([], "numeric"),
    ({"value": 1}, "numeric"),
    ([{"value": 1, "label": "x", "severity": "normal"}] * (MAX_STATES + 1), "numeric"),
    ([{"value": 1, "label": "x"}], "numeric"),
    ([{"value": 1, "label": "x", "severity": "normal", "extra": 1}], "numeric"),
    ([{"value": True, "label": "x", "severity": "normal"}], "numeric"),
    ([{"value": 1.5, "label": "x", "severity": "normal"}], "numeric"),
    ([{"value": 1, "label": "x", "severity": "normal"}], "boolean"),
    ([{"value": "1", "label": "x", "severity": "normal"}], "numeric"),
    ([{"value": 1, "label": "   ", "severity": "normal"}], "numeric"),
    ([{"value": 1, "label": "x" * 101, "severity": "normal"}], "numeric"),
    ([{"value": 1, "label": "x", "severity": "critical"}], "numeric"),
    ([{"value": 1, "label": "a", "severity": "normal"},
      {"value": 1, "label": "b", "severity": "normal"}], "numeric"),
    (POWER, "string"),
])
def test_parse_rejects(raw, value_type):
    with pytest.raises(ValueError):
        parse_states(raw, value_type)


def test_states_from_db_none_and_corrupt():
    assert states_from_db(None, "numeric") is None
    assert states_from_db([{"value": "bad"}], "numeric") is None


def test_states_roundtrip_json():
    states = parse_states(ERROR, "boolean")
    assert parse_states(json.loads(states_to_json(states)), "boolean") == states


def test_states_to_raw_roundtrip():
    assert states_to_raw(parse_states(POWER, "numeric")) == POWER
    assert states_to_raw(parse_states(ERROR, "boolean")) == ERROR


@pytest.mark.parametrize(("value", "label", "severity"), [
    (0, "От батареи", WARNING),
    (1, "От сети", NORMAL),
    (1.0, "От сети", NORMAL),
    (None, NO_DATA_LABEL, NORMAL),
    (7, "Неизвестное значение (7)", WARNING),
    (1.5, "Неизвестное значение", WARNING),
    ("abc", "Неизвестное значение", WARNING),
])
def test_resolve_display_numeric(value, label, severity):
    states = parse_states(POWER, "numeric")
    assert resolve_display(states, value) == SensorDisplay(label, severity)


def test_resolve_display_boolean():
    states = parse_states(ERROR, "boolean")
    assert resolve_display(states, True) == SensorDisplay("Ошибка", ALERT)
    assert resolve_display(states, False) == SensorDisplay("Норма", NORMAL)


def test_resolve_display_without_states():
    assert resolve_display(None, 1) is None


def test_fixed_value_key():
    assert fixed_value_key(True, "boolean") == "true"
    assert fixed_value_key(0, "numeric") == "0"


@pytest.mark.parametrize(("raw", "expected"), [(1, "1"), ("1", "1"), (" 0 ", "0"), (1.0, "1")])
def test_canonical_fixed_value(raw, expected):
    assert canonical_fixed_value(parse_states(POWER, "numeric"), "numeric", raw) == expected


@pytest.mark.parametrize("raw", [5, "x", 1.5, True])
def test_canonical_fixed_value_rejects(raw):
    with pytest.raises(ValueError):
        canonical_fixed_value(parse_states(POWER, "numeric"), "numeric", raw)
