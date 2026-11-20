"""Real-DB integration тесты LockerRepository.update_door — COR-17.

update_door вызывается из CommandResultApplier по позднему ACK устройства.
Если шкаф архивировали (state='archived') между выдачей команды и её ACK,
UPDATE обязан фильтровать state='active', иначе door/last_opened_at/
who_open_last перепишутся на архивной строке.

Unit-тесты с моком db.fetch_one не ловят отсутствие фильтра: mock возвращает
строку независимо от SQL. Этот тест прогоняет реальный UPDATE через Postgres
и проверяет, что архивная строка остаётся нетронутой.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import psycopg2

from app.locker_data.db.models.locker import DoorState
from app.locker_data.db.repositories.locker import LockerRepository
from shared.database import AsyncPostgres, PostgresConfig


def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud"),
    )


def _sync_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _insert_locker(conn, inv_suffix: str, state: str = "active") -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lockers (inventory_number, state, door)
            VALUES (%s, %s, 'closed')
            RETURNING id
            """,
            (f"DOOR-INT-{inv_suffix}", state),
        )
        return str(cur.fetchone()[0])


def _set_archived(conn, locker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE lockers SET state = 'archived' WHERE id = %s",
            (locker_id,),
        )


def _fetch_door_fields(conn, locker_id: str):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT door, last_opened_at, who_open_last FROM lockers WHERE id = %s",
            (locker_id,),
        )
        row = cur.fetchone()
        return {"door": row[0], "last_opened_at": row[1], "who_open_last": row[2]}


def _delete_locker(conn, locker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))


async def _update_door_async(locker_id: str, door: DoorState, who_open_last=None):
    db = AsyncPostgres(_db_config())
    try:
        repo = LockerRepository(db)
        return await repo.update_door(locker_id, door, who_open_last=who_open_last)
    finally:
        db.close()


def test_update_door_archived_locker_not_mutated():
    """Архивный шкаф: update_door(OPEN) → None, door/last_opened_at не меняются."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker_id = _insert_locker(conn, suffix)
        conn.commit()
        _set_archived(conn, locker_id)
        conn.commit()

        before = _fetch_door_fields(conn, locker_id)
        assert before["door"] == "closed"
        assert before["last_opened_at"] is None
        assert before["who_open_last"] is None

        result = asyncio.run(
            _update_door_async(locker_id, DoorState.OPEN, who_open_last=42)
        )
        assert result is None, f"Expected None for archived locker, got {result}"

        # Строка должна остаться нетронутой (psycopg2 не видит uncommitted-
        # данных другого соединения; читаем свежим SELECT).
        conn.rollback()
        after = _fetch_door_fields(conn, locker_id)
        assert after["door"] == "closed"
        assert after["last_opened_at"] is None
        assert after["who_open_last"] is None
    finally:
        _delete_locker(conn, locker_id)
        conn.commit()
        conn.close()


def test_update_door_active_locker_open_happy_path():
    """Активный шкаф: update_door(OPEN, who_open_last=42) → дверь открыта,
    last_opened_at проставлен, who_open_last записан."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker_id = _insert_locker(conn, suffix)
        conn.commit()

        result = asyncio.run(
            _update_door_async(locker_id, DoorState.OPEN, who_open_last=42)
        )
        assert result is not None
        assert result.door == DoorState.OPEN
        assert result.last_opened_at is not None
        assert result.who_open_last == 42

        conn.rollback()
        after = _fetch_door_fields(conn, locker_id)
        assert after["door"] == "open"
        assert after["last_opened_at"] is not None
        assert after["who_open_last"] == 42
    finally:
        _delete_locker(conn, locker_id)
        conn.commit()
        conn.close()
