"""Real-DB integration тесты LockerRepository.sync_door_state.

sync_door_state — телеметрический путь (ingest heartbeat/stat). Регрессия
подтверждённого на стенде бага: heartbeat-stat каждые ~5 минут повторяет
залипшее door=open, а ingest гонял КОМАНДНЫЙ update_door(OPEN), который на
каждый heartbeat сбрасывал last_opened_at=now() и затирал who_open_last=NULL —
«дверь сама переоткрывается каждые 5 минут», нельзя вычислить время с момента
открытия.

Unit-тесты с моком db.fetch_one проверяют лишь SQL-строку. Этот тест прогоняет
реальный UPDATE через Postgres и доказывает, что телеметрический sync НЕ трогает
open-метаданные, выставленные командным путём.
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


def _insert_open_locker(conn, inv_suffix: str, who_open_last: int) -> str:
    """Активный шкаф, уже открытый командой: door=open, last_opened_at и
    who_open_last выставлены (как это делает командный update_door)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lockers (inventory_number, state, door, last_opened_at, who_open_last)
            VALUES (%s, 'active', 'open', now(), %s)
            RETURNING id
            """,
            (f"SYNC-INT-{inv_suffix}", who_open_last),
        )
        return str(cur.fetchone()[0])


def _insert_closed_locker(conn, inv_suffix: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lockers (inventory_number, state, door)
            VALUES (%s, 'active', 'closed')
            RETURNING id
            """,
            (f"SYNC-INT-{inv_suffix}",),
        )
        return str(cur.fetchone()[0])


def _fetch(conn, locker_id: str):
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


async def _sync_async(locker_id: str, door: DoorState):
    db = AsyncPostgres(_db_config())
    try:
        return await LockerRepository(db).sync_door_state(locker_id, door)
    finally:
        db.close()


def test_sync_open_while_already_open_preserves_open_metadata():
    """Heartbeat-stat с door=open на УЖЕ открытом шкафу: last_opened_at и
    who_open_last не меняются, дверь остаётся open. Edge-trigger (door<>open)
    не матчит → строка не трогается вовсе (0 строк, None)."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker_id = _insert_open_locker(conn, suffix, who_open_last=42)
        conn.commit()
        before = _fetch(conn, locker_id)
        assert before["door"] == "open"
        assert before["last_opened_at"] is not None
        assert before["who_open_last"] == 42

        result = asyncio.run(_sync_async(locker_id, DoorState.OPEN))
        # Состояние не изменилось → edge-trigger 0 строк → None.
        assert result is None

        conn.rollback()
        after = _fetch(conn, locker_id)
        assert after["door"] == "open"
        # КЛЮЧЕВОЕ: время открытия и кто открыл сохранены (баг был в их сбросе).
        assert after["last_opened_at"] == before["last_opened_at"]
        assert after["who_open_last"] == 42
    finally:
        _delete_locker(conn, locker_id)
        conn.commit()
        conn.close()


def test_sync_close_transition_does_not_touch_open_metadata():
    """Телеметрия door=closed на открытом шкафу: дверь → closed, но last_opened_at
    и who_open_last сохраняются (атрибуция последнего открытия не теряется)."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker_id = _insert_open_locker(conn, suffix, who_open_last=7)
        conn.commit()
        before = _fetch(conn, locker_id)

        result = asyncio.run(_sync_async(locker_id, DoorState.CLOSED))
        assert result is not None  # реальная смена состояния open→closed
        assert result.door == DoorState.CLOSED

        conn.rollback()
        after = _fetch(conn, locker_id)
        assert after["door"] == "closed"
        assert after["last_opened_at"] == before["last_opened_at"]
        assert after["who_open_last"] == 7
    finally:
        _delete_locker(conn, locker_id)
        conn.commit()
        conn.close()


def test_sync_open_transition_does_not_set_open_metadata():
    """Телеметрия door=open на закрытом шкафу: дверь → open, но last_opened_at и
    who_open_last НЕ проставляются — их владелец только командный путь. Честно:
    если открытие пришло лишь телеметрией (без команды), «когда/кто» неизвестно."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker_id = _insert_closed_locker(conn, suffix)
        conn.commit()

        result = asyncio.run(_sync_async(locker_id, DoorState.OPEN))
        assert result is not None
        assert result.door == DoorState.OPEN

        conn.rollback()
        after = _fetch(conn, locker_id)
        assert after["door"] == "open"
        assert after["last_opened_at"] is None
        assert after["who_open_last"] is None
    finally:
        _delete_locker(conn, locker_id)
        conn.commit()
        conn.close()
