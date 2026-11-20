"""Фикстуры для интеграционных тестов ACM."""
from datetime import datetime, timedelta
import os

import psycopg2
import pytest
import requests

from shared.test_fixtures import wait_for_api as _wait_for_api, wait_for_db
from shared.utils import hash_token

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8004")

# Конфигурация БД пользователей (scud_users) — ACM читает отсюда
USER_DB = {
    "host": os.getenv("USER_DB_HOST", "postgres_user"),
    "port": int(os.getenv("USER_DB_PORT", 5432)),
    "user": os.getenv("USER_DB_USER", "admin"),
    "password": os.getenv("USER_DB_PASSWORD", "password"),
    "dbname": "scud_users",
}

# SEC-24: подключение к scud_users под least-privilege ролью acm_ro (SELECT-only).
# Хост/порт те же, что у USER_DB, отличаются только учётные данные.
ACM_RO_DB = {
    "host": os.getenv("USER_DB_HOST", "postgres_user"),
    "port": int(os.getenv("USER_DB_PORT", 5432)),
    "user": os.getenv("ACM_RO_DB_USER", "acm_ro"),
    "password": os.getenv("ACM_RO_DB_PASSWORD", "acm_ro_dev_password"),
    "dbname": "scud_users",
}

# Конфигурация БД аутентификации (scud_auth) — для вставки тестовых токенов
AUTH_DB = {
    "host": os.getenv("AUTH_DB_HOST", "postgres_auth"),
    "port": int(os.getenv("AUTH_DB_PORT", 5432)),
    "user": os.getenv("AUTH_DB_USER", "admin"),
    "password": os.getenv("AUTH_DB_PASSWORD", "password"),
    "dbname": "scud_auth",
}

# Тестовые пользователи
TEST_WORKER_ID = 2001
TEST_OPERATOR_ID = 2002
TEST_ADMIN_ID = 2003
TEST_LOCKER_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"

# Тестовые токены (plain text — ACM валидирует через auth_api)
TOKEN_WORKER = "acm-test-worker-token-abcdefghijklmnop"
TOKEN_OPERATOR = "acm-test-operator-token-abcdefghijklmn"
TOKEN_ADMIN = "acm-test-admin-token-abcdefghijklmnop"
TOKEN_DEACTIVATED = "acm-test-deactivated-token-abcdefghij"
TEST_DEACTIVATED_ID = 2004


@pytest.fixture(scope="session", autouse=True)
def setup_test_data():
    """Создаёт тестовых пользователей, токены и Допуски в БД."""
    # Ждём готовности обеих БД и нужных таблиц.
    wait_for_db(**USER_DB, probe_table="users")
    wait_for_db(**AUTH_DB, probe_table="access_tokens")

    user_conn = psycopg2.connect(**USER_DB)
    auth_conn = psycopg2.connect(**AUTH_DB)
    try:
        with user_conn.cursor() as cur:
            # worker без Допусков
            cur.execute(
                """
                INSERT INTO users (id, phone, full_name, state, role)
                VALUES (%s, '+79009001001', 'ACM Test Worker', 'active', 'worker')
                ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, role=EXCLUDED.role
                """,
                (TEST_WORKER_ID,),
            )
            # operator
            cur.execute(
                """
                INSERT INTO users (id, phone, full_name, state, role)
                VALUES (%s, '+79009001002', 'ACM Test Operator', 'active', 'operator')
                ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, role=EXCLUDED.role
                """,
                (TEST_OPERATOR_ID,),
            )
            # administrator
            cur.execute(
                """
                INSERT INTO users (id, phone, full_name, state, role)
                VALUES (%s, '+79009001003', 'ACM Test Admin', 'active', 'administrator')
                ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, role=EXCLUDED.role
                """,
                (TEST_ADMIN_ID,),
            )
            # deactivated user
            cur.execute(
                """
                INSERT INTO users (id, phone, full_name, state, role)
                VALUES (%s, '+79009001004', 'ACM Test Deactivated', 'deactivated', 'worker')
                ON CONFLICT (id) DO UPDATE SET state=EXCLUDED.state, role=EXCLUDED.role
                """,
                (TEST_DEACTIVATED_ID,),
            )
            # Допуск для worker на конкретный шкаф
            cur.execute(
                """
                INSERT INTO user_accesses (user_id, start_at, end_at, granted_by, full_access, is_active)
                VALUES (%s, NOW() - INTERVAL '1 hour', NOW() + INTERVAL '1 day', %s, false, true)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                (TEST_WORKER_ID, TEST_OPERATOR_ID),
            )
            row = cur.fetchone()
            if row:
                access_id = row[0]
                cur.execute(
                    """
                    INSERT INTO user_access_lockers (user_access_id, locker_id)
                    VALUES (%s, %s)
                    ON CONFLICT DO NOTHING
                    """,
                    (access_id, TEST_LOCKER_UUID),
                )
            user_conn.commit()

        # Токены в auth БД
        expires_at = datetime.utcnow() + timedelta(hours=24)
        with auth_conn.cursor() as cur:
            for token, uid in [
                (TOKEN_WORKER, TEST_WORKER_ID),
                (TOKEN_OPERATOR, TEST_OPERATOR_ID),
                (TOKEN_ADMIN, TEST_ADMIN_ID),
                (TOKEN_DEACTIVATED, TEST_DEACTIVATED_ID),
            ]:
                cur.execute(
                    """
                    INSERT INTO access_tokens (token_hash, user_id, expires_at)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (token_hash) DO UPDATE
                        SET expires_at = EXCLUDED.expires_at,
                            user_id = EXCLUDED.user_id,
                            revoked_at = NULL
                    """,
                    (hash_token(token), uid, expires_at),
                )
            auth_conn.commit()

    finally:
        user_conn.close()
        auth_conn.close()


@pytest.fixture(scope="session")
def wait_for_acm():
    """Ожидает доступности ACM (probe — /health)."""
    _wait_for_api(
        base_url=BASE_URL,
        probe_path="/health",
        timeout=int(os.getenv("TEST_API_TIMEOUT", "90")),
        accepted_statuses=(200, 503),
    )


@pytest.fixture
def client(wait_for_acm):
    """HTTP-клиент для ACM /acm/check."""

    class AcmClient:
        def __init__(self):
            self.base_url = BASE_URL
            self.session = requests.Session()

        def check(self, token: str, method: str, path: str) -> requests.Response:
            return self.session.get(
                f"{self.base_url}/acm/check",
                headers={
                    "X-Access-Token": token,
                    "X-Request-Method": method,
                    "X-Request-Path": path,
                },
            )

        def check_public(self, method: str, path: str) -> requests.Response:
            return self.session.get(
                f"{self.base_url}/acm/check",
                headers={
                    "X-Request-Method": method,
                    "X-Request-Path": path,
                },
            )

        def identity(self, token: str) -> requests.Response:
            return self.session.get(
                f"{self.base_url}/acm/identity",
                headers={"X-Access-Token": token},
            )

        def identity_public(self) -> requests.Response:
            return self.session.get(f"{self.base_url}/acm/identity")

    return AcmClient()
