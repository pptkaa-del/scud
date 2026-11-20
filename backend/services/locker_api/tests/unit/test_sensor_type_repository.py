"""SensorTypeRepository: чтение states и их запись в update."""
import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from app.locker_data.db.models.sensor import SensorDisplaySeverity
from app.locker_data.db.models.sensor_type import SensorStateOption
from app.locker_data.db.repositories.sensor_type import SensorTypeRepository, _to_sensor_type

_ROW = {"id": 4, "code": "power", "name": "Питание", "description": None, "unit": None,
        "value_type": "numeric", "created_at": datetime.now(), "states": None}


def test_row_states_parsed_into_options():
    row = {**_ROW, "states": [{"value": 1, "label": "От сети", "severity": "normal"}]}
    sensor_type = _to_sensor_type(row)
    assert sensor_type.states == [SensorStateOption(1, "От сети", SensorDisplaySeverity.NORMAL)]


def test_update_keep_states_does_not_write_states():
    db = MagicMock()
    db.fetch_one = AsyncMock(side_effect=[_ROW, _ROW])
    asyncio.run(SensorTypeRepository(db).update(1, name="x"))
    sql, params = db.fetch_one.await_args_list[1].args[:2]
    assert "states = " not in sql


def test_update_states_none_clears():
    db = MagicMock()
    db.fetch_one = AsyncMock(side_effect=[_ROW, _ROW])
    asyncio.run(SensorTypeRepository(db).update(1, states=None))
    sql, params = db.fetch_one.await_args_list[1].args[:2]
    assert "states = %s::jsonb" in sql
    assert None in params


def test_update_states_written_as_json():
    db = MagicMock()
    db.fetch_one = AsyncMock(side_effect=[_ROW, _ROW])
    states = [SensorStateOption(1, "От сети", SensorDisplaySeverity.NORMAL)]
    asyncio.run(SensorTypeRepository(db).update(1, states=states))
    sql, params = db.fetch_one.await_args_list[1].args[:2]
    assert "states = %s::jsonb" in sql
    assert any(isinstance(p, str) and "От сети" in p for p in params)


def test_create_passes_states_param():
    db = MagicMock()
    db.fetch_one_strict = AsyncMock(return_value=_ROW)
    asyncio.run(SensorTypeRepository(db).create("power", "Питание", "numeric"))
    sql, params = db.fetch_one_strict.await_args.args[:2]
    assert "states" in sql
    assert params[-1] is None
