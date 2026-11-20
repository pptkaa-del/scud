"""Real-DB integration тесты атомарного single-use consume (SEC-12).

Проверяют то, что unit-тесты с моком db не могут: что
``StepUpChallengeRepository.consume`` и ``OtpRepository.consume`` — это
single-use CAS-UPDATE, и при двух параллельных вызовах ровно ОДИН успевает
«сжечь» токен, а второй получает отказ. Если кто-то заменит атомарный
``UPDATE ... WHERE used_at IS NULL RETURNING`` на read-then-write, этот тест
поймает double-spend.

Запускается в auth_api_tests контейнере: scud_auth уже поднят миграцией.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import psycopg2

from app.auth_data.db.repositories.otp_repository import OtpRepository
from app.auth_data.db.repositories.step_up import StepUpChallengeRepository
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


def _insert_challenge(conn, *, ttl_seconds: int = 300) -> str:
    challenge_id = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO step_up_challenges
                (id, user_id, scope_kind, scope_payload, code_hash,
                 max_attempts, expires_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (challenge_id, 4242, "locker_command", "{}", "deadbeef", 5, expires_at),
        )
    conn.commit()
    return challenge_id


def _insert_otp(conn, *, ttl_seconds: int = 300) -> int:
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    phone = f"+7900{uuid.uuid4().int % 10000000:07d}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO otp_codes (phone, code_hash, expires_at)
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (phone, "deadbeef", expires_at),
        )
        otp_id = int(cur.fetchone()[0])
    conn.commit()
    return otp_id


def test_step_up_consume_is_single_use_under_concurrency() -> None:
    """Два параллельных consume одного challenge → ровно один успех."""
    async def _run() -> list[bool]:
        config = _db_config()
        conn = _sync_conn()
        try:
            challenge_id = _insert_challenge(conn)
        finally:
            conn.close()

        async def _one() -> bool:
            db = AsyncPostgres(config)
            try:
                return await StepUpChallengeRepository(db).consume(challenge_id)
            finally:
                db.close()

        return await asyncio.gather(_one(), _one())

    results = asyncio.run(_run())
    assert sum(1 for r in results if r) == 1, (
        f"expected exactly one successful consume, got {results}"
    )


def test_step_up_consume_rejects_already_used() -> None:
    """Повторный consume уже использованного challenge → False."""
    async def _run() -> tuple[bool, bool]:
        config = _db_config()
        conn = _sync_conn()
        try:
            challenge_id = _insert_challenge(conn)
        finally:
            conn.close()
        db = AsyncPostgres(config)
        try:
            first = await StepUpChallengeRepository(db).consume(challenge_id)
            second = await StepUpChallengeRepository(db).consume(challenge_id)
        finally:
            db.close()
        return first, second

    first, second = asyncio.run(_run())
    assert first is True
    assert second is False


def test_step_up_consume_rejects_expired() -> None:
    """consume истёкшего challenge → False (expires_at в фильтре)."""
    async def _run() -> bool:
        config = _db_config()
        conn = _sync_conn()
        try:
            challenge_id = _insert_challenge(conn, ttl_seconds=-10)
        finally:
            conn.close()
        db = AsyncPostgres(config)
        try:
            return await StepUpChallengeRepository(db).consume(challenge_id)
        finally:
            db.close()

    assert asyncio.run(_run()) is False


def test_otp_consume_is_single_use_under_concurrency() -> None:
    """Два параллельных consume одного OTP → ровно один успех."""
    async def _run() -> list[bool]:
        config = _db_config()
        conn = _sync_conn()
        try:
            otp_id = _insert_otp(conn)
        finally:
            conn.close()

        async def _one() -> bool:
            db = AsyncPostgres(config)
            try:
                return await OtpRepository(db).consume(otp_id)
            finally:
                db.close()

        return await asyncio.gather(_one(), _one())

    results = asyncio.run(_run())
    assert sum(1 for r in results if r) == 1, (
        f"expected exactly one successful consume, got {results}"
    )
