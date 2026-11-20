"""Smoke-проверка, что step_up_challenges таблица создана init-скриптом.

Падает до создания таблицы; зеленеет после правки 001_init.sql.
Запускается в auth_api_tests контейнере: scud_auth уже поднят с
запущенной миграцией.
"""
from __future__ import annotations

import asyncio
import os

import psycopg2


def _connect():
    return psycopg2.connect(
        host=os.environ["DB_HOST"],
        port=int(os.environ.get("DB_PORT", 5432)),
        user=os.environ.get("DB_USER", "postgres"),
        password=os.environ.get("DB_PASSWORD", "postgres"),
        dbname=os.environ.get("DB_NAME", "scud_auth"),
    )


def test_step_up_challenges_table_exists() -> None:
    async def _check() -> None:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_name = 'step_up_challenges'
                ORDER BY ordinal_position
                """
            )
            cols = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
        assert cols, "step_up_challenges table not found"
        assert cols["id"] == ("uuid", "NO")
        assert cols["user_id"] == ("bigint", "NO")
        assert cols["scope_kind"] == ("character varying", "NO")
        assert cols["scope_payload"] == ("jsonb", "NO")
        assert cols["code_hash"] == ("character varying", "NO")
        assert cols["attempts"] == ("smallint", "NO")
        assert cols["max_attempts"] == ("smallint", "NO")
        assert cols["expires_at"] == ("timestamp with time zone", "NO")
        assert cols["used_at"] == ("timestamp with time zone", "YES")
        assert cols["created_at"] == ("timestamp with time zone", "NO")

    asyncio.run(_check())


def test_step_up_challenges_partial_indexes_exist() -> None:
    async def _check() -> None:
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT indexname FROM pg_indexes
                WHERE tablename = 'step_up_challenges'
                ORDER BY indexname
                """
            )
            names = {r[0] for r in cur.fetchall()}
        assert "idx_step_up_challenges_user_active" in names
        assert "idx_step_up_challenges_expires" in names

    asyncio.run(_check())
