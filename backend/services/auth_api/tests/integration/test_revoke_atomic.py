"""Real-DB integration тесты атомарного revoke_by_user_id (COR-30).

revoke_by_user_id раньше делал count-then-update (два запроса) — гонка между
SELECT COUNT и UPDATE позволяла двум параллельным revoke дважды посчитать
одни и те же токены. Теперь это один атомарный UPDATE ... RETURNING, и сумма
возвращённых count по всем конкурентным вызовам равна числу реально активных
токенов.

Запускается в auth_api_tests контейнере: scud_auth уже поднят миграцией.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import psycopg2

from app.auth_data.db.repositories.token_repository import TokenRepository
from shared.database import AsyncPostgres, PostgresConfig

_N_TOKENS = 5


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


def _seed_tokens(conn, user_id: int, n: int) -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    with conn.cursor() as cur:
        for _ in range(n):
            cur.execute(
                """
                INSERT INTO access_tokens (user_id, token_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (user_id, (uuid.uuid4().hex + uuid.uuid4().hex)[:64],
                 expires_at),
            )
    conn.commit()


def _active_count(conn, user_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM access_tokens "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        return int(cur.fetchone()[0])


def _unique_user_id() -> int:
    # Уникальный user_id в пределах bigint, чтобы тесты не пересекались.
    return 900_000_000 + (uuid.uuid4().int % 50_000_000)


def test_revoke_by_user_id_returns_correct_count() -> None:
    """revoke_by_user_id возвращает число реально отозванных токенов."""
    async def _run() -> tuple[int, int]:
        config = _db_config()
        user_id = _unique_user_id()
        conn = _sync_conn()
        try:
            _seed_tokens(conn, user_id, _N_TOKENS)
        finally:
            conn.close()

        db = AsyncPostgres(config)
        try:
            revoked = await TokenRepository(db).revoke_by_user_id(user_id)
        finally:
            db.close()

        conn2 = _sync_conn()
        try:
            remaining = _active_count(conn2, user_id)
        finally:
            conn2.close()
        return revoked, remaining

    revoked, remaining = asyncio.run(_run())
    assert revoked == _N_TOKENS, f"expected {_N_TOKENS} revoked, got {revoked}"
    assert remaining == 0, f"expected 0 active tokens after revoke, got {remaining}"


def test_revoke_by_user_id_concurrent_does_not_double_count() -> None:
    """Два параллельных revoke суммарно отзывают ровно N токенов."""
    async def _run() -> int:
        config = _db_config()
        user_id = _unique_user_id()
        conn = _sync_conn()
        try:
            _seed_tokens(conn, user_id, _N_TOKENS)
        finally:
            conn.close()

        async def _one() -> int:
            db = AsyncPostgres(config)
            try:
                return await TokenRepository(db).revoke_by_user_id(user_id)
            finally:
                db.close()

        results = await asyncio.gather(_one(), _one())
        return sum(results)

    total = asyncio.run(_run())
    assert total == _N_TOKENS, (
        f"concurrent revoke double-counted: sum={total}, expected {_N_TOKENS}"
    )
