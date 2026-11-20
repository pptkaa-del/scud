"""Real-DB integration тесты LockerRepository light projections.

Покрывают get_operational_status — лёгкую проекцию operational_status,
которую горячий MQTT-путь использует для подавления телеметрии в MAINTENANCE
без SELECT * + dacite-гидрации всей модели на каждый heartbeat-stat.

Тесты идут напрямую через AsyncPostgres (как остальные integration-тесты
locker_api), без HTTP-сервиса. Фикстура `db` отдаёт AsyncPostgres и закрывает
пул; `seeded_locker_id` вставляет активный локер и удаляет его после теста.
"""
from __future__ import annotations

import asyncio
import os
import uuid

import psycopg2
import pytest

from app.locker_data.db.models.locker import OperationalStatus
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


@pytest.fixture
def db():
    """AsyncPostgres-пул для real-DB тестов; закрывается после теста."""
    pg = AsyncPostgres(_db_config())
    try:
        yield pg
    finally:
        pg.close()


@pytest.fixture
def locker_repo(db):
    """LockerRepository, подключённый к тестовой БД."""
    return LockerRepository(db)


@pytest.fixture
def seeded_locker_id():
    """Вставляет активный шкаф и удаляет его после теста."""
    inv = f"REPO-TEST-{uuid.uuid4().hex[:8]}"
    conn = _sync_conn()
    locker_id: str | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lockers (inventory_number, state, door)
                VALUES (%s, 'active', 'closed')
                RETURNING id
                """,
                (inv,),
            )
            locker_id = str(cur.fetchone()[0])
        conn.commit()
        yield locker_id
    finally:
        if locker_id is not None:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
            conn.commit()
        conn.close()


def test_get_operational_status_returns_enum(locker_repo, seeded_locker_id):
    async def scenario():
        status = await locker_repo.get_operational_status(seeded_locker_id)
        assert status == OperationalStatus.NORMAL
        await locker_repo.update_operational_status(seeded_locker_id, OperationalStatus.MAINTENANCE)
        assert await locker_repo.get_operational_status(seeded_locker_id) == OperationalStatus.MAINTENANCE
        assert await locker_repo.get_operational_status("00000000-0000-0000-0000-000000000000") is None
    asyncio.run(scenario())
