"""Фикстуры для интеграционных тестов notification_api."""
import os

import pytest

from shared.test_fixtures import APIClient, wait_for_api as _wait_for_api, wait_for_db

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8005")
REMOTE_USER_ID = "1"

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": os.getenv("DB_NAME", "scud_notifications"),
}


@pytest.fixture(scope="session", autouse=True)
def setup_test_data():
    """Ожидание готовности БД и проверка миграций."""
    wait_for_db(**DB_CONFIG, probe_table="notifications")


@pytest.fixture(scope="session")
def wait_for_api():
    """Ожидание доступности Notification API."""
    _wait_for_api(
        base_url=BASE_URL,
        probe_path="/api/v1/notifications",
        headers={"X-Remote-User": REMOTE_USER_ID},
        timeout=int(os.getenv("TEST_API_TIMEOUT", "90")),
        accepted_statuses=(200, 401),
    )


@pytest.fixture
def api_client(wait_for_api):
    """Клиент для интеграционных тестов."""
    return APIClient(base_url=BASE_URL, default_headers={"X-Remote-User": REMOTE_USER_ID})
