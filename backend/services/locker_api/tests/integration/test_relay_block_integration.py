"""Блок реле: locker_relays + GET/PATCH /locker/{id}/relays на реальной БД."""
from __future__ import annotations

import asyncio
import json
import os
import uuid

import psycopg2
from psycopg2 import errors as pg_errors
import pytest

from app.locker_data.db.repositories.locker import LockerRepository
from app.locker_data.db.repositories.relay import RelayRepository
from shared.database import AsyncPostgres, PostgresConfig

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": os.getenv("DB_NAME", "scud"),
}


def _run(call):
    async def go():
        db = AsyncPostgres(PostgresConfig(
            host=DB_CONFIG["host"], port=DB_CONFIG["port"], user=DB_CONFIG["user"],
            password=DB_CONFIG["password"], database=DB_CONFIG["dbname"],
        ))
        try:
            return await call(db)
        finally:
            db.close()
    return asyncio.run(go())


def _conn():
    conn = psycopg2.connect(**DB_CONFIG)
    conn.autocommit = True
    return conn


def _insert_locker(conn, *, has_relays=None, board_version=None, connectivity="online") -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lockers (inventory_number, has_relays, board_version, connectivity,
                                 last_seen_at)
            VALUES (%s, %s, %s, %s, now())
            RETURNING id::text
            """,
            (f"RELAY-INT-{uuid.uuid4().hex[:8]}", has_relays, board_version, connectivity),
        )
        return str(cur.fetchone()[0])


def _insert_command(conn, locker_id: str, type_: str, relay: int, status: str = "pending") -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO locker_commands (locker_id, type, parameters, status)
            VALUES (%s, %s, %s::jsonb, %s)
            RETURNING id::text
            """,
            (locker_id, type_, json.dumps({"relay": relay}), status),
        )
        return str(cur.fetchone()[0])


@pytest.fixture
def conn():
    c = _conn()
    yield c
    c.close()


@pytest.fixture
def relay_locker(conn):
    locker_id = _insert_locker(conn, has_relays=True, board_version=2)

    async def seed(db):
        repo = RelayRepository(db)
        await repo.ensure_channels(locker_id)
        await repo.set_reported(locker_id, {1: 1, 2: 0, 3: 2, 4: 0})
    _run(seed)
    return locker_id


def test_ensure_channels_is_idempotent_and_set_reported_tracks_changes(conn):
    locker_id = _insert_locker(conn, has_relays=True, board_version=1)

    async def first_pass(db):
        repo = RelayRepository(db)
        await repo.ensure_channels(locker_id)
        await repo.ensure_channels(locker_id)
        await repo.set_reported(locker_id, {1: 1, 2: 0})
        return await repo.list_for_locker(locker_id)

    async def second_pass(db):
        repo = RelayRepository(db)
        await repo.set_reported(locker_id, {1: 1, 2: 1})
        return await repo.list_for_locker(locker_id)

    first = {r.channel: r for r in _run(first_pass)}
    assert sorted(first) == [1, 2, 3, 4]
    assert first[1].reported_state == 1
    assert first[3].reported_state is None
    assert first[3].changed_at is None
    assert first[1].control_enabled is True
    assert first[1].is_protected is False

    second = {r.channel: r for r in _run(second_pass)}
    assert second[1].changed_at == first[1].changed_at
    assert second[2].reported_state == 1
    assert second[2].changed_at is not None
    assert first[2].changed_at is not None
    assert second[2].changed_at >= first[2].changed_at


def test_update_board_info_writes_only_on_change(conn):
    locker_id = _insert_locker(conn)

    async def scenario(db):
        repo = LockerRepository(db)
        changes = [
            await repo.update_board_info(locker_id, 2, True),
            await repo.update_board_info(locker_id, 2, True),
            await repo.update_board_info(locker_id, 2, False),
        ]
        return changes, await repo.get_by_id(locker_id)

    changes, locker = _run(scenario)
    assert changes == [True, False, True]
    assert locker is not None
    assert locker.board_version == 2
    assert locker.has_relays is False


def test_one_active_command_per_channel(conn, relay_locker):
    _insert_command(conn, relay_locker, "relay_on", 1)
    _insert_command(conn, relay_locker, "relay_off", 2)
    _insert_command(conn, relay_locker, "relay_off", 1, status="executed")
    with pytest.raises(pg_errors.UniqueViolation):
        _insert_command(conn, relay_locker, "relay_restart", 1)


def test_locker_detail_exposes_board_fields(api_client, relay_locker):
    resp = api_client.get(f"/api/v1/locker/{relay_locker}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["board_version"] == 2
    assert body["has_relays"] is True


def test_get_relays_lists_channels_for_operator(api_client, conn, relay_locker):
    cmd_id = _insert_command(conn, relay_locker, "relay_off", 1, status="sent")
    resp = api_client.get(
        f"/api/v1/locker/{relay_locker}/relays", headers={"X-Remote-Role": "operator"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["locker_id"] == relay_locker
    assert body["has_relays"] is True
    assert body["connectivity"] == "online"
    assert body["reported_at"] is not None
    items = body["items"]
    assert [i["channel"] for i in items] == [1, 2, 3, 4]
    assert [i["reported_state"] for i in items] == [1, 0, 2, 0]
    assert items[0]["pending_command"] == {"id": cmd_id, "type": "relay_off", "status": "sent"}
    assert items[1]["pending_command"] is None
    assert items[1]["allowed_actions"] == ["relay_on", "relay_off", "relay_restart"]


def test_get_relays_empty_for_board_without_stat(api_client, conn):
    locker_id = _insert_locker(conn)
    body = api_client.get(f"/api/v1/locker/{locker_id}/relays").json()
    assert body["has_relays"] is None
    assert body["board_version"] is None
    assert body["items"] == []


def test_get_relays_worker_needs_grant(api_client, relay_locker):
    denied = api_client.get(
        f"/api/v1/locker/{relay_locker}/relays",
        headers={"X-Remote-Role": "worker", "X-Remote-Lockers": ""})
    assert denied.status_code == 403
    allowed = api_client.get(
        f"/api/v1/locker/{relay_locker}/relays",
        headers={"X-Remote-Role": "worker", "X-Remote-Lockers": relay_locker})
    assert allowed.status_code == 200
    assert all(i["allowed_actions"] == [] for i in allowed.json()["items"])


def test_patch_relay_protects_channel_for_operator(api_client, relay_locker):
    resp = api_client.patch(
        f"/api/v1/locker/{relay_locker}/relays/2",
        json={"name": "Насос", "purpose": "Подкачка", "is_protected": True})
    assert resp.status_code == 200
    item = resp.json()
    assert item["channel"] == 2
    assert item["name"] == "Насос"
    assert item["is_protected"] is True

    listed = api_client.get(
        f"/api/v1/locker/{relay_locker}/relays", headers={"X-Remote-Role": "operator"}).json()
    channel2 = next(i for i in listed["items"] if i["channel"] == 2)
    assert channel2["allowed_actions"] == []
    assert channel2["name"] == "Насос"


def test_patch_relay_disable_and_clear_name(api_client, relay_locker):
    api_client.patch(f"/api/v1/locker/{relay_locker}/relays/3", json={"name": "Свет"})
    resp = api_client.patch(
        f"/api/v1/locker/{relay_locker}/relays/3",
        json={"name": None, "control_enabled": False})
    assert resp.status_code == 200
    item = resp.json()
    assert item["name"] is None
    assert item["control_enabled"] is False
    assert item["allowed_actions"] == []


def test_patch_relay_errors(api_client, conn, relay_locker):
    base = f"/api/v1/locker/{relay_locker}/relays"
    assert api_client.patch(
        f"{base}/1", json={"is_active": False}, headers={"X-Remote-Role": "operator"},
    ).status_code == 403
    assert api_client.patch(f"{base}/1", json={"is_active": "no"}).status_code == 422
    assert api_client.patch(f"{base}/1", json={}).status_code == 422
    assert api_client.patch(f"{base}/5", json={"is_active": False}).status_code == 404
    no_rows = _insert_locker(conn)
    assert api_client.patch(
        f"/api/v1/locker/{no_rows}/relays/1", json={"is_active": False}).status_code == 404
    assert api_client.patch(
        f"/api/v1/locker/{uuid.uuid4()}/relays/1", json={"is_active": False}).status_code == 404


def test_cascade_on_hard_delete(conn, relay_locker):
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lockers WHERE id = %s", (relay_locker,))
        cur.execute("SELECT count(*) FROM locker_relays WHERE locker_id = %s", (relay_locker,))
        assert cur.fetchone()[0] == 0
