"""sensor_types.states: идемпотентный seed-блок 001_init.sql, CHECK на массив,
правка states через PUT /sensor-types и её подпись в display датчика."""
from __future__ import annotations

import json
import os
from pathlib import Path
import uuid

import psycopg2
import psycopg2.errors

import app

_INIT_SQL = Path(app.__file__).parent / "locker_data" / "db" / "migrations" / "001_init.sql"
_BEGIN, _END = "-- sensor-type-states:begin", "-- sensor-type-states:end"


def _block() -> str:
    text = _INIT_SQL.read_text(encoding="utf-8")
    return text[text.index(_BEGIN):text.index(_END)]


def _conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"), port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"), password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _run(sql: str, params: tuple[object, ...] = ()) -> list[tuple[object, ...]]:
    conn = _conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
        return rows
    finally:
        conn.close()


def _states(code: str) -> object:
    return _run("SELECT states FROM sensor_types WHERE code = %s", (code,))[0][0]


def test_seed_has_states_for_enum_types():
    assert [s["label"] for s in _states("power")] == ["От батареи", "От сети"]
    assert [s["label"] for s in _states("net_type")] == ["Не определён", "Ethernet", "GSM"]
    assert [s["value"] for s in _states("error")] == [False, True]
    assert [s["label"] for s in _states("door")] == ["Закрыта", "Открыта"]
    assert _states("battery") is None


def test_states_block_keeps_admin_edit():
    original = _states("power")
    edited = [{"value": 0, "label": "Батарея", "severity": "alert"},
              {"value": 1, "label": "Сеть", "severity": "normal"}]
    try:
        _run("UPDATE sensor_types SET states = %s::jsonb WHERE code = 'power'", (json.dumps(edited),))
        _run(_block())
        _run(_block())
        assert _states("power") == edited
    finally:
        _run("UPDATE sensor_types SET states = %s::jsonb WHERE code = 'power'", (json.dumps(original),))


def test_states_block_refills_cleared_default():
    original = _states("door")
    try:
        _run("UPDATE sensor_types SET states = NULL WHERE code = 'door'")
        _run(_block())
        assert _states("door") == original
    finally:
        _run("UPDATE sensor_types SET states = %s::jsonb WHERE code = 'door'", (json.dumps(original),))


def test_states_block_adds_column_on_old_schema():
    _run("DROP SCHEMA IF EXISTS sts_old CASCADE; CREATE SCHEMA sts_old;")
    try:
        _run("""
            CREATE TABLE sts_old.sensor_types (
                id SERIAL PRIMARY KEY, code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(100) NOT NULL, description TEXT, unit VARCHAR(20),
                value_type VARCHAR(20) NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
            INSERT INTO sts_old.sensor_types (code, name, description, unit, value_type)
            VALUES ('power', 'Питание', 'Датчик напряжения питания', 'V', 'numeric'),
                   ('door', 'Состояние двери', NULL, NULL, 'boolean');
        """)
        _run("SET search_path TO sts_old; " + _block() + _block() + " SET search_path TO public;")
        rows = dict(_run("SELECT code, states FROM sts_old.sensor_types"))
        assert [s["label"] for s in rows["power"]] == ["От батареи", "От сети"]
        assert {"net_type", "error"} <= set(rows)
        power = _run("SELECT description, unit FROM sts_old.sensor_types WHERE code = 'power'")[0]
        assert power == ("Источник питания релейной платы: 0 — от батареи, 1 — от сети", None)
    finally:
        _run("DROP SCHEMA IF EXISTS sts_old CASCADE;")


def test_states_block_keeps_customised_power_row():
    _run("DROP SCHEMA IF EXISTS sts_custom CASCADE; CREATE SCHEMA sts_custom;")
    try:
        _run("""
            CREATE TABLE sts_custom.sensor_types (
                id SERIAL PRIMARY KEY, code VARCHAR(50) NOT NULL UNIQUE,
                name VARCHAR(100) NOT NULL, description TEXT, unit VARCHAR(20),
                value_type VARCHAR(20) NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
            INSERT INTO sts_custom.sensor_types (code, name, description, unit, value_type)
            VALUES ('power', 'Питание', 'Моё описание', 'V', 'numeric');
        """)
        _run("SET search_path TO sts_custom; " + _block() + " SET search_path TO public;")
        power = _run("SELECT description, unit FROM sts_custom.sensor_types WHERE code = 'power'")[0]
        assert power == ("Моё описание", "V")
    finally:
        _run("DROP SCHEMA IF EXISTS sts_custom CASCADE;")


def test_states_check_rejects_non_array():
    conn = _conn()
    try:
        with conn.cursor() as cur:
            try:
                cur.execute("UPDATE sensor_types SET states = '{}'::jsonb WHERE code = 'power'")
            except psycopg2.errors.CheckViolation:
                conn.rollback()
                return
        raise AssertionError("CHECK sensor_types_states_is_array did not fire")
    finally:
        conn.close()


def test_admin_label_visible_on_sensor_read(api_client):
    original = _states("power")
    power_id = _run("SELECT id FROM sensor_types WHERE code = 'power'")[0][0]
    edited = [{"value": 0, "label": "Батарея", "severity": "alert"},
              {"value": 1, "label": "Сеть", "severity": "normal"}]
    locker_id = None
    try:
        locker = api_client.post("/api/v1/lockers", json={
            "inventory_number": f"STS-{uuid.uuid4().hex[:10]}",
            "location_address": "ул. Состояний, 1",
            "transport": "mqtt",
            "relay_id": 500_000_000 + uuid.uuid4().int % 400_000_000,
        })
        assert locker.status_code == 201, locker.text
        locker_id = locker.json()["id"]
        created = api_client.post(f"/api/v1/locker/{locker_id}/sensors", json={
            "serial_number": f"PWR-{uuid.uuid4().hex[:8]}",
            "sensor_type_id": power_id,
            "current_value": 0,
        })
        assert created.status_code == 201, created.text

        updated = api_client.put(f"/api/v1/sensor-types/{power_id}", json={"states": edited})
        assert updated.status_code == 200, updated.text
        assert updated.json()["states"] == edited
        assert _states("power") == edited

        listed = api_client.get(f"/api/v1/locker/{locker_id}/sensors")
        assert listed.status_code == 200, listed.text
        item = next(i for i in listed.json()["items"] if i["sensor_type_id"] == power_id)
        assert item["display"] == {"label": "Батарея", "severity": "alert"}, (_states("power"), listed.text)
    finally:
        _run("UPDATE sensor_types SET states = %s::jsonb WHERE code = 'power'", (json.dumps(original),))
        if locker_id:
            api_client.delete(f"/api/v1/locker/{locker_id}")


def test_range_threshold_of_deleted_sensor_does_not_block_states(api_client):
    code = f"sts_{uuid.uuid4().hex[:10]}"
    states = [{"value": 0, "label": "Выкл", "severity": "normal"},
              {"value": 1, "label": "Вкл", "severity": "warning"}]
    locker_id = None
    try:
        sensor_type = api_client.post("/api/v1/sensor-types", json={
            "code": code, "name": "Режим", "value_type": "numeric"})
        assert sensor_type.status_code == 201, sensor_type.text
        type_id = sensor_type.json()["id"]
        locker = api_client.post("/api/v1/lockers", json={
            "inventory_number": f"STS-{uuid.uuid4().hex[:10]}",
            "location_address": "ул. Состояний, 2",
            "transport": "mqtt",
            "relay_id": 500_000_000 + uuid.uuid4().int % 400_000_000,
        })
        assert locker.status_code == 201, locker.text
        locker_id = locker.json()["id"]
        sensor = api_client.post(f"/api/v1/locker/{locker_id}/sensors", json={
            "serial_number": f"MOD-{uuid.uuid4().hex[:8]}", "sensor_type_id": type_id})
        assert sensor.status_code == 201, sensor.text
        sensor_id = sensor.json()["id"]
        threshold = api_client.post(f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
                                    json={"min_value": 0, "max_value": 5})
        assert threshold.status_code == 201, threshold.text

        blocked = api_client.put(f"/api/v1/sensor-types/{type_id}", json={"states": states})
        assert blocked.status_code == 409, blocked.text

        deleted = api_client.delete(f"/api/v1/locker/{locker_id}/sensors/{sensor_id}")
        assert deleted.status_code == 204, deleted.text

        allowed = api_client.put(f"/api/v1/sensor-types/{type_id}", json={"states": states})
        assert allowed.status_code == 200, allowed.text
        assert allowed.json()["states"] == states
    finally:
        if locker_id:
            api_client.delete(f"/api/v1/locker/{locker_id}")
