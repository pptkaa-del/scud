"""Фикстуры для интеграционных тестов locker_api."""
import os

import pytest

from shared.test_fixtures import APIClient, wait_for_api as _wait_for_api, wait_for_db

# Конфигурация из переменных окружения (для Docker Compose) или дефолты
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8001")
REMOTE_USER_ID = "1"  # ID системного пользователя
TEST_PHONE = "+79001112233"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": os.getenv("DB_NAME", "scud"),
}


@pytest.fixture(scope="session", autouse=True)
def setup_test_data():
    """Ожидание готовности БД (миграции таблицы lockers).

    Пропускает wait_for_db, если SCUD_DB_DOWN=1 — тест намеренно гоняется
    при отключённой БД (деградационный дрилл /ready).
    """
    if os.getenv("SCUD_DB_DOWN") == "1":
        return
    # Системный пользователь не нужен в БД locker_api — аутентификация идёт
    # через заголовок X-Remote-User.
    wait_for_db(**DB_CONFIG, probe_table="lockers")


@pytest.fixture(scope="session")
def wait_for_api():
    """Ожидание доступности Locker API перед запуском тестов."""
    _wait_for_api(
        base_url=BASE_URL,
        probe_path="/api/v1/lockers",
        headers={"X-Remote-User": REMOTE_USER_ID},
        timeout=int(os.getenv("TEST_API_TIMEOUT", "90")),
        accepted_statuses=(200, 401),
    )


@pytest.fixture
def api_client(wait_for_api):
    """Клиент для интеграционных тестов с настроенными заголовками.

    `X-Remote-Role: administrator` подмешивается дефолтно, чтобы integration-
    тесты могли создавать REBOOT/CONFIG-команды без явного указания роли в
    каждом теста. Тесты для конкретных ролей (worker → 403 на reboot, и т.п.)
    переопределяют header через `headers={"X-Remote-Role": "worker"}` в .post().
    """
    return APIClient(
        base_url=BASE_URL,
        default_headers={
            "X-Remote-User": REMOTE_USER_ID,
            "X-Remote-Role": "administrator",
        },
    )
