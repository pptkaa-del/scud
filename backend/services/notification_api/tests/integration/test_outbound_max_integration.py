"""Real-DB integration тесты outbound_max_messages (SEC-18).

Покрывают то, что unit-моки не ловят: что после mark_sent/mark_failed
plaintext OTP (`body`) физически отсутствует в строке, и что purge_expired
удаляет просроченные записи, но НЕ трогает живые pending (их код ещё валиден).
"""
from __future__ import annotations

import asyncio
import os

import psycopg2

from app.notification_data.db.repositories.outbound_max import OutboundMaxRepository
from shared.database import AsyncPostgres, PostgresConfig

_PLAINTEXT = "Код входа: 654321\nДействует 5 мин."


def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud_notifications"),
    )


def _sync_conn():
    conn = psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud_notifications"),
    )
    conn.autocommit = True
    return conn


def _insert(conn, *, ttl_seconds: int, body: str = _PLAINTEXT, user_id: int = 900001) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outbound_max_messages
                (user_id, max_user_id, body, purpose, expires_at)
            VALUES (%s, NULL, %s, 'login', now() + (%s || ' seconds')::interval)
            RETURNING id
            """,
            (user_id, body, ttl_seconds),
        )
        return int(cur.fetchone()[0])


def _body_of(conn, message_id: int) -> object:
    with conn.cursor() as cur:
        cur.execute("SELECT body FROM outbound_max_messages WHERE id = %s", (message_id,))
        row = cur.fetchone()
        return None if row is None else row[0]


def _exists(conn, message_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM outbound_max_messages WHERE id = %s", (message_id,))
        return cur.fetchone() is not None


def _cleanup(conn, user_id: int = 900001) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM outbound_max_messages WHERE user_id = %s", (user_id,))


def test_mark_sent_clears_plaintext_in_db() -> None:
    conn = _sync_conn()
    try:
        _cleanup(conn)
        msg_id = _insert(conn, ttl_seconds=300)
        assert _body_of(conn, msg_id) == _PLAINTEXT  # plaintext до отправки

        async def _run() -> None:
            db = AsyncPostgres(_db_config())
            try:
                await OutboundMaxRepository(db).mark_sent(msg_id)
            finally:
                db.close()

        asyncio.run(_run())
        assert _body_of(conn, msg_id) is None, "OTP plaintext must be gone after mark_sent"
    finally:
        _cleanup(conn)
        conn.close()


def test_mark_failed_clears_plaintext_in_db() -> None:
    conn = _sync_conn()
    try:
        _cleanup(conn)
        msg_id = _insert(conn, ttl_seconds=300)

        async def _run() -> None:
            db = AsyncPostgres(_db_config())
            try:
                await OutboundMaxRepository(db).mark_failed(msg_id, "max returned 403")
            finally:
                db.close()

        asyncio.run(_run())
        assert _body_of(conn, msg_id) is None, "OTP plaintext must be gone after mark_failed"
    finally:
        _cleanup(conn)
        conn.close()


def test_purge_deletes_expired_but_keeps_live_pending() -> None:
    conn = _sync_conn()
    try:
        _cleanup(conn)
        expired_id = _insert(conn, ttl_seconds=-10)   # уже просрочен
        live_id = _insert(conn, ttl_seconds=3600)     # живой pending, код ещё валиден

        async def _run() -> None:
            db = AsyncPostgres(_db_config())
            try:
                await OutboundMaxRepository(db).purge_expired()
            finally:
                db.close()

        asyncio.run(_run())
        assert not _exists(conn, expired_id), "expired row must be purged"
        assert _exists(conn, live_id), "live pending row must survive purge"
    finally:
        _cleanup(conn)
        conn.close()
