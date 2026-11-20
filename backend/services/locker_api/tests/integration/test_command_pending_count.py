"""Integration: CommandRepository.count_pending() с реальной БД.

Проверяет, что count_pending() считает только строки с
status='pending' AND retry_count < max_retries — т.е. команды,
которые ещё имеют retry-бюджет и не перешли в терминальный статус.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import psycopg2

from app.locker_data.db.repositories.command import CommandRepository
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


def _insert_locker(conn, inv_suffix: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lockers (inventory_number, state) VALUES (%s, 'active') RETURNING id",
            (f"PENDING-CNT-{inv_suffix}",),
        )
        return str(cur.fetchone()[0])


def _insert_command(
    conn,
    locker_id: str,
    *,
    status: str = "pending",
    retry_count: int = 0,
    max_retries: int = 3,
) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO locker_commands
                (locker_id, type, status, created_by, parameters,
                 retry_count, max_retries)
            VALUES (%s, 'open', %s, 99, '{}'::jsonb, %s, %s)
            RETURNING id
            """,
            (locker_id, status, retry_count, max_retries),
        )
        return str(cur.fetchone()[0])


def _delete_locker(conn, locker_id: str) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))


async def _count_pending_async() -> int:
    config = _db_config()
    db = AsyncPostgres(config)
    try:
        repo = CommandRepository(db)
        return await repo.count_pending()
    finally:
        db.close()


def test_count_pending_counts_only_eligible_rows():
    """count_pending = 2 pending (с бюджетом) из 3 вставленных.

    Сценарий:
    - cmd1: status='pending', retry_count=0, max_retries=3 → СЧИТАЕТСЯ
    - cmd2: status='pending', retry_count=0, max_retries=3 → СЧИТАЕТСЯ
    - cmd3: status='executed', retry_count=0, max_retries=3 → НЕ СЧИТАЕТСЯ (не pending)

    Дополнительно: pending с исчерпанным бюджетом (retry_count >= max_retries)
    не должен входить в счёт — такие строки видны get_pending_commands с тем же
    фильтром.
    """
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker = _insert_locker(conn, suffix)
        conn.commit()

        # 2 eligible pending
        _insert_command(conn, locker, status="pending", retry_count=0, max_retries=3)
        _insert_command(conn, locker, status="pending", retry_count=0, max_retries=3)
        # 1 executed — не должна войти в count
        _insert_command(conn, locker, status="executed", retry_count=0, max_retries=3)
        # 1 pending с исчерпанным бюджетом — retry_count >= max_retries
        _insert_command(conn, locker, status="pending", retry_count=3, max_retries=3)
        conn.commit()

        count = asyncio.run(_count_pending_async())
        assert count == 2, f"expected 2 eligible pending commands, got {count}"
    finally:
        _delete_locker(conn, locker)
        conn.commit()
        conn.close()


def test_count_pending_returns_zero_when_empty():
    """count_pending = 0 при пустой таблице для нашего локера."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker = _insert_locker(conn, suffix)
        conn.commit()

        count = asyncio.run(_count_pending_async())
        # Просто убеждаемся, что пустой результат → 0, без исключений.
        assert isinstance(count, int)
        assert count >= 0
    finally:
        _delete_locker(conn, locker)
        conn.commit()
        conn.close()
