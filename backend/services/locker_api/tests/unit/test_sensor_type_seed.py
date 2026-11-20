"""Seed-блок states в 001_init.sql проходит тот же валидатор, что и API."""
import json
from pathlib import Path
import re

import app
from app.locker_data.db.models.sensor_type import SensorCode
from app.mqtt import ingest
from app.mqtt.relay_protocol import NetType, PowerSource
from app.sensor_states import parse_states

_INIT_SQL = Path(app.__file__).parent / "locker_data" / "db" / "migrations" / "001_init.sql"
_UPDATE = re.compile(
    r"UPDATE sensor_types SET states = '(?P<json>\[.*?\])'::jsonb\s+"
    r"WHERE code = '(?P<code>\w+)' AND states IS NULL;",
    re.S,
)
_VALUE_TYPES = {
    "power": "numeric", "net_type": "numeric", "error": "boolean", "door": "boolean",
    "intrusion": "boolean",
}


def test_seed_states_are_valid():
    found = {m["code"]: json.loads(m["json"]) for m in _UPDATE.finditer(_INIT_SQL.read_text("utf-8"))}
    assert set(found) == set(_VALUE_TYPES)
    for code, raw in found.items():
        parse_states(raw, _VALUE_TYPES[code])


def _seed_values(code):
    found = {m["code"]: json.loads(m["json"]) for m in _UPDATE.finditer(_INIT_SQL.read_text("utf-8"))}
    return {state["value"] for state in found[code]}


def test_every_firmware_enum_member_is_labelled():
    assert _seed_values("power") == {int(m) for m in PowerSource}
    assert _seed_values("net_type") == {int(m) for m in NetType}


def test_ingest_sensor_codes_are_sensor_code_members():
    codes = {name: value for name, value in vars(ingest).items() if name.startswith("_SENSOR_")}
    assert codes
    for name, value in codes.items():
        assert isinstance(value, SensorCode), name


def test_no_code_registry_left():
    assert not (Path(app.__file__).parent / "sensor_registry.py").exists()
