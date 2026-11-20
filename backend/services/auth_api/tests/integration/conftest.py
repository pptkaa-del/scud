"""Фикстуры для интеграционных тестов auth_api."""
from datetime import datetime, timedelta
import hashlib
import os

import bcrypt
import psycopg2
import pytest
import requests

from shared.test_fixtures import APIClient, wait_for_api as _wait_for_api, wait_for_db

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8003")

TEST_PHONE = "+79001112244"
TEST_PASSWORD = "testpassword123"
TEST_PASSWORD_HASH = hashlib.sha256(TEST_PASSWORD.encode("utf-8")).hexdigest()
TEST_PASSWORD_BCRYPT = bcrypt.hashpw(TEST_PASSWORD_HASH.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
TEST_USER_ID = 1001

# Администратор (require_2fa=False — cookie выдаётся сразу при /login)
ADMIN_PHONE = "+79001112255"
ADMIN_PASSWORD = "adminpassword123"
ADMIN_PASSWORD_HASH = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest()
ADMIN_PASSWORD_BCRYPT = bcrypt.hashpw(ADMIN_PASSWORD_HASH.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
ADMIN_USER_ID = 1002

# DB конфигурация для auth_api (scud_auth)
AUTH_DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": os.getenv("DB_NAME", "scud_auth"),
}

# DB конфигурация для user_api (scud_users) — отдельный хост/порт
USER_DB = {
    "host": os.getenv("USER_DB_HOST", "postgres_user"),
    "port": int(os.getenv("USER_DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": "scud_users",
}

# DB конфигурация для history_api (scud_history) — используется в тестах
# проверки публикации security_events из step_up хендлеров.
HISTORY_DB = {
    "host": os.getenv("HISTORY_DB_HOST", "postgres_history"),
    "port": int(os.getenv("HISTORY_DB_PORT", 5432)),
    "user": os.getenv("DB_USER", "admin"),
    "password": os.getenv("DB_PASSWORD", "password"),
    "dbname": "scud_history",
}


@pytest.fixture(scope="session", autouse=True)
def setup_test_data():
    """
    Создаёт тестового пользователя в user_api БД с паролем,
    ждёт готовности обеих БД.
    """
    # Ждём auth БД и наличия таблицы otp_codes (миграции).
    wait_for_db(**AUTH_DB, probe_table="otp_codes")
    # Ждём user БД и наличия таблицы users.
    wait_for_db(**USER_DB, probe_table="users")
    # Ждём history БД (для проверок публикации security_events).
    wait_for_db(**HISTORY_DB, probe_table="security_events")

    user_conn = psycopg2.connect(**USER_DB)
    try:
        with user_conn.cursor() as cur:
            # Тестовый worker
            cur.execute(
                """
                INSERT INTO users (id, phone, full_name, state, role)
                VALUES (%s, %s, 'Auth Test User', 'active', 'worker')
                ON CONFLICT (id) DO UPDATE
                    SET phone = EXCLUDED.phone,
                        full_name = EXCLUDED.full_name,
                        state = EXCLUDED.state,
                        role = EXCLUDED.role
                """,
                (TEST_USER_ID, TEST_PHONE),
            )
            cur.execute(
                """
                INSERT INTO user_auth (user_id, password_hash, require_2fa)
                VALUES (%s, %s, TRUE)
                ON CONFLICT (user_id) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        require_2fa = EXCLUDED.require_2fa
                """,
                (TEST_USER_ID, TEST_PASSWORD_BCRYPT),
            )
            # Тестовый администратор (require_2fa=False — cookie при логине без OTP)
            cur.execute(
                """
                INSERT INTO users (id, phone, full_name, state, role)
                VALUES (%s, %s, 'Auth Test Admin', 'active', 'administrator')
                ON CONFLICT (id) DO UPDATE
                    SET phone = EXCLUDED.phone,
                        full_name = EXCLUDED.full_name,
                        state = EXCLUDED.state,
                        role = EXCLUDED.role
                """,
                (ADMIN_USER_ID, ADMIN_PHONE),
            )
            cur.execute(
                """
                INSERT INTO user_auth (user_id, password_hash, require_2fa)
                VALUES (%s, %s, FALSE)
                ON CONFLICT (user_id) DO UPDATE
                    SET password_hash = EXCLUDED.password_hash,
                        require_2fa = EXCLUDED.require_2fa
                """,
                (ADMIN_USER_ID, ADMIN_PASSWORD_BCRYPT),
            )
            user_conn.commit()
    finally:
        user_conn.close()


def insert_otp(phone: str, code: str, ttl_seconds: int = 300) -> None:
    """Вставляет OTP напрямую в БД для тестирования verify endpoint."""
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    expires_at = datetime.utcnow() + timedelta(seconds=ttl_seconds)
    conn = psycopg2.connect(**AUTH_DB)
    try:
        with conn.cursor() as cur:
            # Инвалидируем предыдущие активные OTP для этого телефона
            cur.execute(
                "UPDATE otp_codes SET used_at = now() WHERE phone = %s AND used_at IS NULL",
                (phone,),
            )
            cur.execute(
                """
                INSERT INTO otp_codes (phone, code_hash, expires_at)
                VALUES (%s, %s, %s)
                """,
                (phone, code_hash, expires_at),
            )
            conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="session")
def wait_for_api():
    """Ожидает доступности auth_api (POST-проба /login c заведомо неверным паролем)."""
    _wait_for_api(
        base_url=BASE_URL,
        probe_path="/api/v1/auth/login",
        timeout=int(os.getenv("TEST_API_TIMEOUT", "90")),
        accepted_statuses=(200, 401),
        method="POST",
        json_body={"phone": "+70000000000", "password": "x"},
    )


@pytest.fixture
def api_client(wait_for_api):
    """HTTP клиент для auth_api тестов."""
    return APIClient(base_url=BASE_URL)


@pytest.fixture
def admin_session(wait_for_api):
    """
    Сессия администратора. Логин без OTP (require_2fa=False) — cookie устанавливается
    сразу в ответе на /login.
    """
    session = requests.Session()
    response = session.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200, f"admin login failed: {response.text}"
    assert "access_token" in session.cookies, "access_token cookie not set after admin login"
    return session


@pytest.fixture
def seeded_user_with_2fa_no_max(wait_for_api):
    """Создаёт пользователя в scud_users с require_2fa=True, без max_user_id.

    Используется step-up integration-тестом: позволяет проверить issue-ветку
    без max-биндинга (SMS/MAX-fallback, т.к. phone NOT NULL) и прямой seed
    challenge в scud_auth для verify, не задействуя реальный Max API.
    """
    conn = psycopg2.connect(**USER_DB)
    user_id = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (full_name, phone, role, state)
                VALUES ('Step-up Test', '+79991112233', 'worker', 'active')
                RETURNING id
                """,
            )
            row = cur.fetchone()
            assert row is not None
            user_id = row[0]
            cur.execute(
                """
                INSERT INTO user_auth (user_id, password_hash, require_2fa)
                VALUES (%s, %s, TRUE)
                """,
                (user_id, "x" * 64),
            )
        conn.commit()
    finally:
        conn.close()
    yield {"user_id": user_id}
    # Cleanup
    conn = psycopg2.connect(**USER_DB)
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_auth WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def auth_session(wait_for_api):
    """
    Полный цикл login → verify, возвращает session с установленным access_token cookie.
    Использует прямую вставку OTP в БД (dev-режим: SMS не отправляется).
    """
    otp_code = "123456"
    insert_otp(TEST_PHONE, otp_code)

    session = requests.Session()
    response = session.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": otp_code},
    )
    assert response.status_code == 200, f"verify failed: {response.text}"
    assert "access_token" in session.cookies, "access_token cookie not set after verify"
    return session
