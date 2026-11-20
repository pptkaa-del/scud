"""Фикстуры для интеграционных тестов user_api."""
import os

import psycopg2
import pytest

from shared.test_fixtures import APIClient, wait_for_api as _wait_for_api, wait_for_db

# Конфигурация из переменных окружения (для Docker Compose) или дефолты
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")
REMOTE_USER_ID = "1"  # ID системного пользователя
TEST_PHONE = "+79001112233"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": os.getenv("DB_NAME", "scud_users"),
}


@pytest.fixture(scope="session", autouse=True)
def setup_test_data():
    """Создание системного пользователя в БД для авторизации тестов."""
    wait_for_db(**DB_CONFIG, probe_table="users")

    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (id, phone, full_name, state, role)
                VALUES (%s, %s, 'System User', 'active', 'administrator')
                ON CONFLICT (id) DO UPDATE
                    SET phone     = EXCLUDED.phone,
                        full_name = EXCLUDED.full_name,
                        state     = EXCLUDED.state
                """,
                (int(REMOTE_USER_ID), TEST_PHONE,)
            )
            cur.execute("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))")
            conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def wait_for_api():
    """Ожидание доступности API перед запуском тестов."""
    _wait_for_api(
        base_url=BASE_URL,
        probe_path=f"/api/v1/user/{REMOTE_USER_ID}",
        headers={"X-Remote-User": REMOTE_USER_ID},
        timeout=int(os.getenv("TEST_API_TIMEOUT", "90")),
        accepted_statuses=(200, 404),
    )


@pytest.fixture
def db_connection():
    """Прямое psycopg2 соединение для проверки состояния БД в тестах."""
    conn = psycopg2.connect(**DB_CONFIG)
    yield conn
    conn.close()


@pytest.fixture
def api_client(wait_for_api):
    """Клиент для интеграционных тестов с настроенными заголовками."""
    return APIClient(base_url=BASE_URL, default_headers={"X-Remote-User": REMOTE_USER_ID})
