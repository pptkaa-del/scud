"""Real-DB гонка полного verify-пути step-up: reserve_attempt → consume.

Существующие test_atomic_consume / test_atomic_attempts проверяют каждый
атомарный метод по отдельности. Здесь фиксируется их связка, как её
использует StepUpVerifyHandler: на каждый verify сначала reserve_attempt
(списать бюджет + получить code_hash), затем — при верном коде — consume
(single-use сжечь challenge).

Инварианты (SEC-12 + SEC-13):
- При N параллельных verify с ВЕРНЫМ кодом против одного challenge consume
  выигрывает ровно один (single-use), остальные получают отказ — даже если
  все они успели зарезервировать попытку.
- При неверном коде reserve_attempt расходует бюджет ровно один раз на
  попытку; lockout наступает РОВНО на max_attempts, не раньше и не позже —
  при конкуренции счётчик не теряет инкременты и не перепрыгивает порог.

Запускается в auth_api_tests контейнере: scud_auth поднят миграцией.
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
import uuid

import psycopg2

from app.auth_data.db.repositories.step_up import StepUpChallengeRepository
from shared.database import AsyncPostgres, PostgresConfig

_CODE_HASH = "c0ffee"
_MAX_ATTEMPTS = 3
_PARALLEL = 12


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


def _insert_challenge(conn, *, max_attempts: int = _MAX_ATTEMPTS) -> str:
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
            (challenge_id, 4242, "locker_command", "{}", _CODE_HASH,
             max_attempts, expires_at),
        )
    conn.commit()
    return challenge_id


async def _verify_once(config: PostgresConfig, challenge_id: str) -> bool:
    """Эмулирует verify-путь: reserve_attempt (верный code_hash) → consume.

    Возвращает True только если consume сжёг challenge (verify «успешен»).
    """
    db = AsyncPostgres(config)
    try:
        repo = StepUpChallengeRepository(db)
        reserved = await repo.reserve_attempt(challenge_id)
        if reserved is None:
            return False  # бюджет исчерпан / уже использован
        # Код верный (code_hash совпадает) → пытаемся сжечь single-use.
        return await repo.consume(challenge_id)
    finally:
        db.close()


def test_correct_code_consume_single_winner_under_concurrency() -> None:
    """N параллельных verify с верным кодом → ровно один consume успешен."""
    async def _run() -> list[bool]:
        config = _db_config()
        conn = _sync_conn()
        try:
            challenge_id = _insert_challenge(conn, max_attempts=_PARALLEL)
        finally:
            conn.close()
        return await asyncio.gather(
            *[_verify_once(config, challenge_id) for _ in range(_PARALLEL)]
        )

    results = asyncio.run(_run())
    assert sum(1 for ok in results if ok) == 1, (
        f"expected exactly one successful verify/consume, got {results}"
    )


def test_wrong_code_lockout_exactly_at_threshold() -> None:
    """Неверный код: reserve_attempt разрешён РОВНО max_attempts раз, затем
    lockout. При конкуренции счётчик не теряет инкременты и не перепрыгивает
    порог.
    """
    async def _run() -> tuple[int, int]:
        config = _db_config()
        conn = _sync_conn()
        try:
            challenge_id = _insert_challenge(conn, max_attempts=_MAX_ATTEMPTS)
        finally:
            conn.close()

        async def _reserve() -> bool:
            db = AsyncPostgres(config)
            try:
                # Неверный код: reserve_attempt всё равно списывает бюджет
                # (инкремент происходит до сравнения), но consume не зовём.
                reserved = await StepUpChallengeRepository(db).reserve_attempt(
                    challenge_id,
                )
                return reserved is not None
            finally:
                db.close()

        results = await asyncio.gather(*[_reserve() for _ in range(_PARALLEL)])
        granted = sum(1 for r in results if r)

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
        f"lockout должен наступить ровно на {_MAX_ATTEMPTS}, granted={granted}"
    )
    assert final_attempts == _MAX_ATTEMPTS, (
        f"attempts перепрыгнул порог: {final_attempts}"
    )
