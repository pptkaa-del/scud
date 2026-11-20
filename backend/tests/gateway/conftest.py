"""Фикстуры для интеграционных тестов через Apache Gateway."""
import hashlib
import os
import random
import time

import bcrypt
import psycopg2
import psycopg2.extras
import pytest
import requests

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://localhost")
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "+79000000000")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
TEST_TIMEOUT = int(os.getenv("TEST_API_TIMEOUT", "120"))

USER_DB = {
    "host": os.getenv("USER_DB_HOST", "postgres_user"),
    "port": int(os.getenv("USER_DB_PORT", 5432)),
    "user": os.getenv("USER_DB_USER", "admin"),
    "password": os.getenv("USER_DB_PASSWORD", "password"),
    "dbname": os.getenv("USER_DB_NAME", "scud_users"),
}

# Фиктивный UUID шкафа для ACM-тестов (не обязан существовать в locker_api)
ACM_TEST_LOCKER_UUID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


def unique_phone() -> str:
    """Генерирует уникальный российский телефонный номер для тестов."""
    return "+7" + str(random.randint(9000000000, 9999999999))


def _connect_user_db(max_retries: int = 30) -> psycopg2.extensions.connection:
    for _ in range(max_retries):
        try:
            return psycopg2.connect(**USER_DB)
        except psycopg2.OperationalError:
            time.sleep(2)
    pytest.fail(f"Could not connect to user database {USER_DB['dbname']}")


@pytest.fixture(scope="session", autouse=True)
def setup_test_data():
    """
    Ждёт появления admin-пользователя в БД (создаётся user_api на старте)
    и гарантирует корректный user_auth: правильный пароль и require_2fa=False.

    Это необходимо потому что:
    - user_api создаёт admin асинхронно при старте
    - при повторных запусках user_auth может быть в неверном состоянии
      (user_api не обновляет пароль если пользователь уже существует)
    """
    password_hash = hashlib.sha256(ADMIN_PASSWORD.encode("utf-8")).hexdigest()
    bcrypt_hash = bcrypt.hashpw(password_hash.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    start = time.time()
    while time.time() - start < TEST_TIMEOUT:
        try:
            conn = _connect_user_db(max_retries=1)
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute("SELECT id FROM users WHERE phone = %s", (ADMIN_PHONE,))
                row = cur.fetchone()
            if row:
                admin_id = row["id"]
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO user_auth (user_id, password_hash, require_2fa)
                        VALUES (%s, %s, FALSE)
                        ON CONFLICT (user_id) DO UPDATE
                            SET password_hash = EXCLUDED.password_hash,
                                require_2fa   = EXCLUDED.require_2fa
                        """,
                        (admin_id, bcrypt_hash),
                    )
                conn.commit()
                conn.close()
                return
            conn.close()
        except psycopg2.OperationalError:
            pass
        time.sleep(2)

    pytest.fail(f"Admin user {ADMIN_PHONE} not found in DB after {TEST_TIMEOUT}s")


@pytest.fixture(scope="session")
def base_url() -> str:
    return GATEWAY_URL


@pytest.fixture(scope="session")
def wait_for_gateway(setup_test_data, base_url):
    """
    Ожидает доступности gateway.
    Использует GET /api/v1/users (без cookie → 401 от Apache Lua),
    чтобы не расходовать rate limit login-эндпоинта.
    """
    start = time.time()
    while time.time() - start < TEST_TIMEOUT:
        try:
            resp = requests.get(f"{base_url}/api/v1/users", timeout=5)
            if resp.status_code in (200, 401, 403):
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(2)
    pytest.fail(f"Gateway at {base_url} not reachable after {TEST_TIMEOUT}s")


@pytest.fixture(scope="session")
def admin_session(wait_for_gateway, base_url) -> requests.Session:
    """
    Аутентифицированная сессия администратора (session-scoped).
    Один логин на весь прогон тестов — не расходует rate limit.
    """
    session = requests.Session()
    resp = session.post(
        f"{base_url}/api/v1/auth/login",
        json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200, (
        f"Admin login failed: {resp.status_code} {resp.text}"
    )
    assert "access_token" in session.cookies, (
        "access_token cookie not set after admin login"
    )
    yield session
    session.post(f"{base_url}/api/v1/auth/logout")


@pytest.fixture(scope="class")
def worker_session(admin_session, base_url):
    """
    Создаёт worker-пользователя с паролем, логинит его через gateway.
    Возвращает (session, user_id). Очищает ресурсы после теста.
    """
    phone = unique_phone()
    resp = admin_session.post(
        f"{base_url}/api/v1/users",
        json={"phone": phone, "full_name": "ACM Test Worker",
              "password": "workerpass1", "role": "worker"},
    )
    assert resp.status_code == 201, f"Failed to create worker: {resp.text}"
    user_id = resp.json()["id"]

    # user_api выставляет require_2fa=True по умолчанию — отключаем для тестов
    conn = _connect_user_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_auth SET require_2fa = FALSE WHERE user_id = %s",
                (user_id,),
            )
        conn.commit()
    finally:
        conn.close()

    session = requests.Session()
    login = session.post(
        f"{base_url}/api/v1/auth/login",
        json={"phone": phone, "password": "workerpass1"},
    )
    assert login.status_code == 200, f"Worker login failed: {login.text}"
    assert "access_token" in session.cookies

    yield session, user_id

    session.post(f"{base_url}/api/v1/auth/logout")
    admin_session.delete(f"{base_url}/api/v1/user/{user_id}")


def _create_worker_with_grants(admin_session, base_url, locker_ids, full_name):
    """
    Создаёт worker-пользователя с Допуском на `locker_ids` и логинит его.

    Допуски вставляются в БД ДО логина — ACM увидит их при первом же запросе.
    Порядок принципиален: ACM кэширует контекст пользователя (TTL — 30 с),
    поэтому Допуск, выданный после первого запроса, до истечения кэша
    не виден. По той же причине каждый вызов создаёт СВОЕГО пользователя,
    а не переиспользует существующего (иначе — stale-кэш без Допуска).

    Возвращает (session, user_id, access_id).
    """
    phone = unique_phone()
    resp = admin_session.post(
        f"{base_url}/api/v1/users",
        json={"phone": phone, "full_name": full_name,
              "password": "workerpass1", "role": "worker"},
    )
    assert resp.status_code == 201, (
        f"Failed to create worker {full_name!r}: {resp.text}"
    )
    user_id = resp.json()["id"]

    conn = _connect_user_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_auth SET require_2fa = FALSE WHERE user_id = %s",
                (user_id,),
            )
            cur.execute(
                """
                INSERT INTO user_accesses
                    (user_id, start_at, end_at, granted_by, full_access, is_active)
                VALUES (%s, NOW() - INTERVAL '1 hour', NOW() + INTERVAL '1 day',
                        %s, false, true)
                RETURNING id
                """,
                (user_id, user_id),
            )
            access_id = cur.fetchone()[0]
            for locker_id in locker_ids:
                cur.execute(
                    "INSERT INTO user_access_lockers (user_access_id, locker_id)"
                    " VALUES (%s, %s)",
                    (access_id, locker_id),
                )
        conn.commit()
    finally:
        conn.close()

    session = requests.Session()
    login = session.post(
        f"{base_url}/api/v1/auth/login",
        json={"phone": phone, "password": "workerpass1"},
    )
    assert login.status_code == 200, f"Worker {full_name!r} login failed: {login.text}"
    assert "access_token" in session.cookies

    return session, user_id, access_id


def _cleanup_worker_with_grants(admin_session, base_url, session, user_id, access_id):
    """Разлогинивает worker'а, удаляет пользователя и его Допуски."""
    session.post(f"{base_url}/api/v1/auth/logout")
    admin_session.delete(f"{base_url}/api/v1/user/{user_id}")

    conn = _connect_user_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_access_lockers WHERE user_access_id = %s",
                (access_id,),
            )
            cur.execute("DELETE FROM user_accesses WHERE id = %s", (access_id,))
        conn.commit()
    finally:
        conn.close()


def _create_locker(admin_session, base_url, tag: str) -> str:
    """
    Создаёт РЕАЛЬНЫЙ шкаф через gateway и возвращает его UUID.

    relay_id — семизначный, чтобы не конфликтовать с парком эмулятора
    (relay_id 1..1000 и 5001..5030): для transport='mqtt' на relay_id висит
    partial unique index, дубль дал бы 409.
    """
    suffix = random.randint(1000000, 9999999)
    resp = admin_session.post(
        f"{base_url}/api/v1/lockers",
        json={
            "inventory_number": f"ACM-SCOPE-{tag}-{suffix}",
            "transport": "mqtt",
            "relay_id": suffix,
        },
    )
    assert resp.status_code == 201, (
        f"Failed to create locker ({tag}): {resp.status_code} {resp.text}"
    )
    return resp.json()["id"]


@pytest.fixture
def worker_with_access(admin_session, base_url):
    """
    Создаёт отдельного worker-пользователя с Допуском на ACM_TEST_LOCKER_UUID.
    Допуск вставляется в БД до логина — ACM увидит его при первом же запросе.
    Возвращает (session, user_id, locker_uuid). Очищает ресурсы после теста.
    """
    session, user_id, access_id = _create_worker_with_grants(
        admin_session, base_url,
        [ACM_TEST_LOCKER_UUID], "ACM Test Worker With Access",
    )

    yield session, user_id, ACM_TEST_LOCKER_UUID

    _cleanup_worker_with_grants(admin_session, base_url, session, user_id, access_id)


@pytest.fixture
def worker_with_scoped_lockers(admin_session, base_url):
    """
    Worker с Допуском ровно на ОДИН из двух РЕАЛЬНО существующих шкафов.

    В отличие от `worker_with_access` (фиктивный ACM_TEST_LOCKER_UUID, которого
    в locker_api нет), шкафы здесь создаются через API: без реальных строк в
    scud_lockers список GET /api/v1/lockers был бы пуст, и проверять фильтрацию
    было бы не на чем.

    Допуск, как и в `worker_with_access`, вставляется ДО логина (кэш ACM, 30 с).

    Возвращает (session, user_id, granted_locker_id, ungranted_locker_id).
    """
    granted_id = _create_locker(admin_session, base_url, "granted")
    ungranted_id = _create_locker(admin_session, base_url, "ungranted")

    session, user_id, access_id = _create_worker_with_grants(
        admin_session, base_url, [granted_id], "ACM Test Worker Scoped List",
    )

    yield session, user_id, granted_id, ungranted_id

    _cleanup_worker_with_grants(admin_session, base_url, session, user_id, access_id)
    for locker_id in (granted_id, ungranted_id):
        admin_session.delete(f"{base_url}/api/v1/locker/{locker_id}")


@pytest.fixture(scope="class")
def operator_session(admin_session, base_url):
    """
    Создаёт operator-пользователя с паролем, логинит его через gateway.
    Возвращает (session, user_id). Очищает ресурсы после теста.
    """
    phone = unique_phone()
    resp = admin_session.post(
        f"{base_url}/api/v1/users",
        json={"phone": phone, "full_name": "ACM Test Operator",
              "password": "operatorpass1", "role": "operator"},
    )
    assert resp.status_code == 201, f"Failed to create operator: {resp.text}"
    user_id = resp.json()["id"]

    # user_api выставляет require_2fa=True по умолчанию — отключаем для тестов
    conn = _connect_user_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_auth SET require_2fa = FALSE WHERE user_id = %s",
                (user_id,),
            )
        conn.commit()
    finally:
        conn.close()

    session = requests.Session()
    login = session.post(
        f"{base_url}/api/v1/auth/login",
        json={"phone": phone, "password": "operatorpass1"},
    )
    assert login.status_code == 200, f"Operator login failed: {login.text}"
    assert "access_token" in session.cookies

    yield session, user_id

    session.post(f"{base_url}/api/v1/auth/logout")
    admin_session.delete(f"{base_url}/api/v1/user/{user_id}")


@pytest.fixture
def created_user(admin_session, base_url):
    """
    Создаёт тестового пользователя через API и удаляет его после теста.
    """
    phone = unique_phone()
    resp = admin_session.post(
        f"{base_url}/api/v1/users",
        json={"phone": phone, "full_name": "Тест Временный", "password": "testpass123"},
    )
    assert resp.status_code == 201, f"Failed to create test user: {resp.text}"
    user = resp.json()
    yield user
    admin_session.delete(f"{base_url}/api/v1/user/{user['id']}")


@pytest.fixture
def created_contractor(admin_session, base_url):
    """
    Создаёт тестового подрядчика через API и удаляет его после теста.
    """
    resp = admin_session.post(
        f"{base_url}/api/v1/contractors",
        json={"name": "ООО Тест Подрядчик"},
    )
    assert resp.status_code == 201, f"Failed to create contractor: {resp.text}"
    contractor = resp.json()
    yield contractor
    admin_session.delete(f"{base_url}/api/v1/contractor/{contractor['id']}")
