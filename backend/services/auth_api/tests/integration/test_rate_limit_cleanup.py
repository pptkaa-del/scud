"""Real-DB integration тесты очистки login_rate_limit (COR-28).

Таблица login_rate_limit росла неограниченно — для каждого нового IP
создавалась строка, которая никогда не удалялась. delete_stale() удаляет
строки, чьё окно (window_start) старше переданного порога удержания.

Запускается в auth_api_tests контейнере: scud_auth уже поднят миграцией.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import psycopg2

from app.auth_data.db.repositories.rate_limit_repository import RateLimitRepository
from shared.database import AsyncPostgres, PostgresConfig


def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud_auth"),
    )


def _sync_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud_auth"),
    )


def _insert_row(conn, key: str, *, age_seconds: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO login_rate_limit (key, attempts, window_start)
            VALUES (%s, 1, now() - make_interval(secs => %s))
            ON CONFLICT (key) DO UPDATE
                SET window_start = EXCLUDED.window_start
            """,
            (key, age_seconds),
        )
    conn.commit()


def _exists(conn, key: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM login_rate_limit WHERE key = %s", (key,))
        return cur.fetchone() is not None


def test_delete_stale_removes_old_rows_keeps_fresh() -> None:
    """delete_stale удаляет строки старше порога, свежие — оставляет."""
    async def _run() -> tuple[bool, bool]:
        config = _db_config()
        stale_key = f"stale:{uuid.uuid4()}"
        fresh_key = f"fresh:{uuid.uuid4()}"
        conn = _sync_conn()
        try:
            _insert_row(conn, stale_key, age_seconds=3600)
            _insert_row(conn, fresh_key, age_seconds=5)
        finally:
            conn.close()

        db = AsyncPostgres(config)
        try:
            # Порог удержания 600 c: stale (3600 c) удаляется, fresh (5 c) нет.
            await RateLimitRepository(db).delete_stale(600)
        finally:
            db.close()

        conn2 = _sync_conn()
        try:
            return _exists(conn2, stale_key), _exists(conn2, fresh_key)
        finally:
            conn2.close()

    stale_exists, fresh_exists = asyncio.run(_run())
    assert stale_exists is False, "stale rate-limit row was not purged"
    assert fresh_exists is True, "fresh rate-limit row was wrongly purged"
