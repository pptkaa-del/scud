"""Real-DB integration тесты атомарного резервирования попытки (SEC-13).

Проверяют, что ``reserve_attempt`` атомарно расходует бюджет попыток: при N
параллельных неверных попытках против challenge/OTP с max_attempts=M
расходуется не более M попыток, дальнейшие отклоняются (возвращают None).
Без атомарности check→compare→increment гонка позволяла обойти лимит.

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

_MAX_ATTEMPTS = 3
_PARALLEL = 10


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


def _insert_challenge(conn) -> str:
    challenge_id = str(uuid.uuid4())
    expires_at = datetime.now(UTC) + timedelta(seconds=300)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO step_up_challenges
                (id, user_id, scope_kind, scope_payload, code_hash,
                 max_attempts, expires_at)
            VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (challenge_id, 4242, "locker_command", "{}", "feedface",
             _MAX_ATTEMPTS, expires_at),
        )
    conn.commit()
    return challenge_id


def _insert_otp(conn) -> int:
    expires_at = datetime.now(UTC) + timedelta(seconds=300)
    phone = f"+7900{uuid.uuid4().int % 10000000:07d}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO otp_codes (phone, code_hash, attempts, expires_at)
            VALUES (%s, %s, 0, %s)
            RETURNING id
            """,
            (phone, "feedface", expires_at),
        )
        otp_id = int(cur.fetchone()[0])
    conn.commit()
    return otp_id


def test_step_up_reserve_attempt_caps_under_concurrency() -> None:
    """N параллельных reserve_attempt → не более max_attempts успешных."""
    async def _run() -> tuple[int, int]:
        config = _db_config()
        conn = _sync_conn()
        try:
            challenge_id = _insert_challenge(conn)
        finally:
            conn.close()

        async def _one() -> bool:
            db = AsyncPostgres(config)
            try:
                return await StepUpChallengeRepository(db).reserve_attempt(
                    challenge_id,
                ) is not None
            finally:
                db.close()

        results = await asyncio.gather(*[_one() for _ in range(_PARALLEL)])
        granted = sum(1 for r in results if r)

        # Подтверждаем по БД, что attempts не перепрыгнул max_attempts.
        conn2 = _sync_conn()
        try:
            with conn2.cursor() as cur:
                cur.execute(
                    "SELECT attempts FROM step_up_challenges WHERE id = %s",
                    (challenge_id,),
                )
                final_attempts = int(cur.fetchone()[0])
        finally:
            conn2.close()
        return granted, final_attempts

    granted, final_attempts = asyncio.run(_run())
    assert granted == _MAX_ATTEMPTS, (
        f"expected exactly {_MAX_ATTEMPTS} granted, got {granted}"
    )
    assert final_attempts == _MAX_ATTEMPTS, (
        f"attempts overflowed past max: {final_attempts}"
    )


def test_step_up_reserve_attempt_returns_code_hash() -> None:
    """reserve_attempt возвращает code_hash для сравнения кода."""
    async def _run() -> str | None:
        config = _db_config()
        conn = _sync_conn()
        try:
            challenge_id = _insert_challenge(conn)
        finally:
            conn.close()
        db = AsyncPostgres(config)
        try:
            reserved = await StepUpChallengeRepository(db).reserve_attempt(
                challenge_id,
            )
        finally:
            db.close()
        return None if reserved is None else reserved["code_hash"]

    assert asyncio.run(_run()) == "feedface"


def test_otp_reserve_attempt_caps_under_concurrency() -> None:
    """N параллельных reserve_attempt для OTP → не более max_attempts успешных."""
    async def _run() -> int:
        config = _db_config()
        conn = _sync_conn()
        try:
            otp_id = _insert_otp(conn)
        finally:
            conn.close()

        async def _one() -> bool:
            db = AsyncPostgres(config)
            try:
                return await OtpRepository(db).reserve_attempt(
                    otp_id, _MAX_ATTEMPTS,
                ) is not None
            finally:
                db.close()

        results = await asyncio.gather(*[_one() for _ in range(_PARALLEL)])
        return sum(1 for r in results if r)

    granted = asyncio.run(_run())
    assert granted == _MAX_ATTEMPTS, (
        f"expected exactly {_MAX_ATTEMPTS} granted, got {granted}"
    )
