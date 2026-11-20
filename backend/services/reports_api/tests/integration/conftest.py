"""Фикстуры для интеграционных тестов reports_api.

Дополнительно к ожиданию reports_api: параметры подключения к scud_history
(`HISTORY_DB_*`) и хелпер сидирования `command_history` напрямую через psycopg2.
Round-trip тест сидирует историю → ставит отчёт → ждёт worker → скачивает xlsx.
"""
import os
import time
import uuid

import psycopg2
import pytest

from shared.test_fixtures import APIClient, wait_for_api as _wait_for_api

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8006")
REMOTE_USER_ID = "1"

# Подключение к scud_history для сидирования command_history (round-trip тест).
HISTORY_DB_CONFIG = {
    "host": os.getenv("HISTORY_DB_HOST", "localhost"),
    "port": int(os.getenv("HISTORY_DB_PORT", "5432")),
    "user": os.getenv("HISTORY_DB_USER", "admin"),
    "password": os.getenv("HISTORY_DB_PASSWORD", "password"),
    "dbname": os.getenv("HISTORY_DB_NAME", "scud_history"),
}

# Подключение к scud_reports для прямого сидирования report_jobs
# (тест реклейма зависших running-задач, COR-13).
REPORTS_DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": os.getenv("DB_NAME", "scud_reports"),
}


def insert_stuck_running_job(
    *,
    report_type: str,
    params: dict,
    requested_by: int,
    attempts: int,
    started_age_seconds: int,
) -> str:
    """Вставить зависшую running-задачу с устаревшим started_at.

    Эмулирует падение worker'а после flip в running, но до mark_ready/failed.
    Возвращает id созданной задачи.
    """
    import json

    conn = psycopg2.connect(**REPORTS_DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO report_jobs (
                    report_type, params, requested_by, status,
                    attempts, started_at
                ) VALUES (
                    %s, %s::jsonb, %s, 'running',
                    %s, now() - make_interval(secs => %s)
                ) RETURNING id
                """,
                (
                    report_type,
                    json.dumps(params),
                    requested_by,
                    attempts,
                    started_age_seconds,
                ),
            )
            job_id = str(cur.fetchone()[0])
        conn.commit()
        return job_id
    finally:
        conn.close()


def insert_command(
    *,
    command_type: str,
    status: str,
    executed_at: str | None,
    locker_inventory_number: str,
    created_by: int,
    user_full_name: str,
) -> None:
    """Вставить строку command_history с явным executed_at (все NOT NULL поля)."""
    conn = psycopg2.connect(**HISTORY_DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO command_history (
                    command_id, locker_id, locker_inventory_number,
                    command_type, created_by, user_full_name,
                    created_at, status, executed_at
                ) VALUES (%s, %s, %s, %s, %s, %s, now(), %s, %s)
                """,
                (
                    str(uuid.uuid4()),
                    str(uuid.uuid4()),
                    locker_inventory_number,
                    command_type,
                    created_by,
                    user_full_name,
                    status,
                    executed_at,
                ),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session", autouse=True)
def wait_for_history_db():
    """Дождаться готовности scud_history.command_history перед сидированием."""
    max_retries = 30
    for _ in range(max_retries):
        try:
            conn = psycopg2.connect(**HISTORY_DB_CONFIG)
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT EXISTS (SELECT FROM information_schema.tables "
                        "WHERE table_name = 'command_history')"
                    )
                    if cur.fetchone()[0]:
                        return
            finally:
                conn.close()
        except psycopg2.OperationalError:
            pass
        time.sleep(1)
    pytest.fail("scud_history.command_history not ready")


@pytest.fixture(scope="session")
def wait_for_api():
    """Ожидание доступности Reports API."""
    _wait_for_api(
        base_url=BASE_URL,
        probe_path="/health",
        headers={"X-Remote-User": REMOTE_USER_ID},
        timeout=int(os.getenv("TEST_API_TIMEOUT", "90")),
        accepted_statuses=(200,),
    )


@pytest.fixture
def api_client(wait_for_api):
    """Клиент для интеграционных тестов."""
    return APIClient(base_url=BASE_URL, default_headers={"X-Remote-User": REMOTE_USER_ID})
