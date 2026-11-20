"""Фикстуры для интеграционных тестов history_api."""
import json
import os
import time
import uuid

import psycopg2
import pytest
import requests

from shared.test_fixtures import APIClient, wait_for_api as _wait_for_api
from shared.utils import body_sha256, compute_request_signature

# SEC-05: общий с history_api HMAC-ключ для подписи internal-запросов записи.
# None → history_api работает fail-open и подпись не требуется.
INTERNAL_HISTORY_HMAC_KEY = os.getenv("INTERNAL_HISTORY_HMAC_KEY", "") or None

# Конфигурация из переменных окружения (для Docker Compose) или дефолты
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8002")
REMOTE_USER_ID = "1"  # ID системного пользователя

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": os.getenv("DB_NAME", "scud_history"),
}


# Утилиты для генерации уникальных UUID
def generate_unique_locker_id():
    """Генерирует уникальный UUID для шкафа."""
    return str(uuid.uuid4())


def generate_unique_sensor_id():
    """Генерирует уникальный UUID для датчика."""
    return str(uuid.uuid4())


def generate_unique_command_id():
    """Генерирует уникальный UUID для команды."""
    return str(uuid.uuid4())


def post_internal_media(files, data) -> requests.Response:
    """POST /internal/api/v1/media с HMAC-подписью над финальным raw-body (SEC-05).

    requests строит multipart внутри prepare() с собственным boundary; чтобы
    sha256(body) совпал с тем, что увидит history_api, подписываем уже
    собранное тело PreparedRequest. Используется во всех media-upload тестах.
    """
    url = f"{BASE_URL}/internal/api/v1/media"
    prepared = requests.Request("POST", url, files=files, data=data).prepare()
    if INTERNAL_HISTORY_HMAC_KEY is not None:
        raw = prepared.body
        raw_bytes = raw if isinstance(raw, bytes) else (raw or "").encode("utf-8")
        ts = str(int(time.time()))
        prepared.headers["X-Internal-Timestamp"] = ts
        prepared.headers["X-Internal-Signature"] = compute_request_signature(
            timestamp=ts,
            method="POST",
            path="/internal/api/v1/media",
            body_sha256_hex=body_sha256(raw_bytes),
            key=INTERNAL_HISTORY_HMAC_KEY.encode("utf-8"),
        )
    with requests.Session() as session:
        return session.send(prepared)


@pytest.fixture(scope="session", autouse=True)
def setup_test_data():
    """Ожидание готовности БД history_api и нескольких ключевых таблиц.

    History API хранит данные сразу в нескольких таблицах
    (sensor_telemetry, security_events, command_history) — проверяем все три
    разом через information_schema, поскольку shared.wait_for_db умеет ждать
    только одну таблицу за раз.
    """
    max_retries = 30
    conn = None
    for _ in range(max_retries):
        try:
            conn = psycopg2.connect(**DB_CONFIG)
            break
        except psycopg2.OperationalError:
            time.sleep(1)

    if not conn:
        pytest.fail("Could not connect to database for setup")

    try:
        tables_ready = False
        for _ in range(max_retries):
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT (
                            SELECT COUNT(*) FROM information_schema.tables
                            WHERE table_name IN (
                                'sensor_telemetry', 'security_events',
                                'command_history', 'locker_media'
                            )
                        ) = 4
                        """
                    )
                    tables_ready = cur.fetchone()[0]
                    if tables_ready:
                        break
            except psycopg2.Error:
                conn.rollback()
            time.sleep(1)

        if not tables_ready:
            pytest.fail("Database tables not ready after setup (migrations failed or slow)")
    finally:
        conn.close()


@pytest.fixture(scope="session")
def wait_for_api():
    """Ожидание доступности History API перед запуском тестов."""
    _wait_for_api(
        base_url=BASE_URL,
        probe_path="/api/v1/telemetry/list",
        headers={"X-Remote-User": REMOTE_USER_ID},
        timeout=int(os.getenv("TEST_API_TIMEOUT", "90")),
        accepted_statuses=(200, 400, 401),
    )


@pytest.fixture
def api_client(wait_for_api):
    """Клиент для интеграционных тестов с настроенными заголовками."""
    return APIClient(base_url=BASE_URL, default_headers={"X-Remote-User": REMOTE_USER_ID})


def post_internal_security_event(payload: dict) -> requests.Response:
    """POST /internal/api/v1/events/security с HMAC-подписью над JSON-телом (SEC-05)."""
    path = "/internal/api/v1/events/security"
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if INTERNAL_HISTORY_HMAC_KEY is not None:
        ts = str(int(time.time()))
        headers["X-Internal-Timestamp"] = ts
        headers["X-Internal-Signature"] = compute_request_signature(
            timestamp=ts,
            method="POST",
            path=path,
            body_sha256_hex=body_sha256(body),
            key=INTERNAL_HISTORY_HMAC_KEY.encode("utf-8"),
        )
    return requests.post(f"{BASE_URL}{path}", data=body, headers=headers)
