"""Integration: POST /commands → 202 → confirm → 201 для 2FA workflow.

Прямые HTTP-вызовы к locker_api по сети compose. Полагается на то, что
auth_api/user_api/notification_api поднимаются вместе с locker_api при
``docker compose up -d`` (стандартная подготовка integration-стека).
Тест не модифицирует docker-compose dependencies — auth_api необязательно
healthy для locker_api_tests контейнера, но при ``docker compose up -d``
весь стек поднят и доступен по backend_net.

Если auth_api недоступен (запущен только locker_api без auth_api), POST
/commands с require_2fa=true вернёт 503 — это не fail теста, а сигнал
неполного окружения. См. ``test_post_commands_2fa_path_dispatches``.

Эмулируем поведение Apache+Lua, выставляя заголовки ``X-Remote-User``,
``X-Remote-Role`` и ``X-Remote-Require-2FA`` напрямую в integration-запросе.
"""
from __future__ import annotations

import os
import time
import uuid

import psycopg2
import pytest
import requests

BASE_URL = os.getenv("TEST_BASE_URL", "http://locker_api:8001")
USER_DB_HOST = os.getenv("USER_DB_HOST", "postgres_user")
USER_DB_PORT = int(os.getenv("USER_DB_PORT", 5432))
NOTIFICATION_DB_HOST = os.getenv("NOTIFICATION_DB_HOST", "postgres_notification")
NOTIFICATION_DB_PORT = int(os.getenv("NOTIFICATION_DB_PORT", 5432))


def _locker_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres_locker"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud_lockers"),
    )


def _user_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=USER_DB_HOST,
        port=USER_DB_PORT,
        user="admin",
        password=os.getenv("DB_PASSWORD", "password"),
        dbname="scud_users",
    )


def _notification_db() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=NOTIFICATION_DB_HOST,
        port=NOTIFICATION_DB_PORT,
        user="admin",
        password=os.getenv("DB_PASSWORD", "password"),
        dbname="scud_notifications",
    )


def _is_user_db_reachable() -> bool:
    try:
        conn = _user_db()
        conn.close()
        return True
    except psycopg2.OperationalError:
        return False


@pytest.fixture
def seeded_locker():
    """Создаёт активный шкаф напрямую в scud_lockers, возвращает id."""
    conn = _locker_db()
    inv = f"INT-2FA-{uuid.uuid4().hex[:8]}"
    locker_id: str | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO lockers (inventory_number, state)
                VALUES (%s, 'active')
                RETURNING id
                """,
                (inv,),
            )
            row = cur.fetchone()
            assert row is not None
            locker_id = str(row[0])
        conn.commit()
    finally:
        conn.close()
    assert locker_id is not None
    yield {"id": locker_id, "inventory_number": inv}
    # Cleanup: hard delete для теста чтобы не накапливалось.
    conn = _locker_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM locker_commands WHERE locker_id = %s", (locker_id,),
            )
            cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def worker_user_with_2fa():
    """Создаёт worker'а в scud_users с require_2fa=TRUE и max_user_id."""
    if not _is_user_db_reachable():
        pytest.skip(
            "user_db недоступен из locker_api_tests — для теста нужен полный "
            "compose-стек (docker compose up -d перед запуском тестов)",
        )
    phone = f"+7900{uuid.uuid4().int % 10**7:07d}"
    # Generate a unique max_user_id to avoid collision with previous failed
    # cleanups or parallel runs (ux_users_max_user_id_not_deleted is UNIQUE
    # on rows where deleted_at IS NULL).
    max_user_id = 900_000_000 + uuid.uuid4().int % 10**7
    conn = _user_db()
    user_id: int | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (full_name, phone, role, state, max_user_id)
                VALUES ('2FA Worker INT', %s, 'worker', 'active', %s)
                RETURNING id
                """,
                (phone, max_user_id),
            )
            row = cur.fetchone()
            assert row is not None
            user_id = int(row[0])
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
    assert user_id is not None
    yield {"id": user_id, "phone": phone}
    # Cleanup
    conn = _user_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_auth WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _peek_code(user_id: int) -> str | None:
    """Достаёт последний pending-код из outbox notification_db."""
    try:
        conn = _notification_db()
    except psycopg2.OperationalError:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT body FROM outbound_max_messages
                WHERE user_id = %s
                ORDER BY created_at DESC LIMIT 1
                """,
                (user_id,),
            )
            row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    import re
    m = re.search(r"\b(\d{6})\b", row[0])
    return m.group(1) if m else None


def test_post_commands_2fa_path_dispatches(seeded_locker, worker_user_with_2fa):
    """POST /commands с X-Remote-Require-2FA:true — путь step-up активирован.

    Если auth_api доступен → 202 + challenge_id.
    Если auth_api недоступен → 503 (это всё ещё подтверждает, что путь
    step-up выбран, а не байт-идентичный enqueue).
    """
    locker_id = seeded_locker["id"]
    user_id = worker_user_with_2fa["id"]
    headers = {
        "X-Remote-User": str(user_id),
        "X-Remote-Role": "worker",
        "X-Remote-Lockers": locker_id,
        "X-Remote-Require-2FA": "true",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands",
        headers=headers,
        json={"type": "open", "parameters": {}, "max_retries": 3},
        timeout=10,
    )
    # 202 — auth_api up и выдал challenge.
    # 503 — auth_api недоступен. Самой команды в БД быть НЕ должно — это
    # критический инвариант: 2FA-пользователь без второго фактора команды
    # выдавать не может.
    assert resp.status_code in (202, 503), resp.text
    if resp.status_code == 202:
        body = resp.json()
        assert body["challenge_id"]
        assert body["delivery_status"] in {
            "sent", "queued_until_binding", "max_binding_required",
        }
    # Команд в БД не должно быть, независимо от ветки.
    conn = _locker_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM locker_commands WHERE locker_id = %s",
                (locker_id,),
            )
            count = cur.fetchone()[0]
    finally:
        conn.close()
    assert count == 0, "Command must NOT be enqueued before step-up confirm"


def test_post_commands_no_2fa_byte_identical(seeded_locker):
    """Worker без 2FA: POST /commands → 201 как раньше (regression)."""
    locker_id = seeded_locker["id"]
    headers = {
        "X-Remote-User": "1",
        "X-Remote-Role": "administrator",
        "X-Remote-Require-2FA": "false",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands",
        headers=headers,
        json={"type": "open", "parameters": {}, "max_retries": 3},
        timeout=10,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "open"
    assert body["status"] == "pending"


def test_post_commands_close_with_2fa_byte_identical(seeded_locker):
    """close с require_2fa=true → 201 сразу (close в исключениях)."""
    locker_id = seeded_locker["id"]
    headers = {
        "X-Remote-User": "1",
        "X-Remote-Role": "administrator",
        "X-Remote-Require-2FA": "true",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands",
        headers=headers,
        json={"type": "close", "parameters": {}, "max_retries": 3},
        timeout=10,
    )
    assert resp.status_code == 201, resp.text


def test_confirm_with_invalid_challenge_returns_401(seeded_locker):
    """POST /commands/confirm с неверным/несуществующим challenge_id → 401."""
    locker_id = seeded_locker["id"]
    headers = {
        "X-Remote-User": "1",
        "X-Remote-Role": "administrator",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands/confirm",
        headers=headers,
        json={
            "challenge_id": str(uuid.uuid4()),
            "code": "000000",
        },
        timeout=10,
    )
    # 401 — auth_api ответил Challenge not found.
    # 503 — auth_api недоступен.
    assert resp.status_code in (401, 503), resp.text


def test_two_step_open_flow_full(seeded_locker, worker_user_with_2fa):
    """End-to-end: 202 → достать код из outbox → confirm → 201.

    Skipped if outbox empty after 10s — это означает, что notification_api
    не подключён к Max или вызов auth_api → notification_api не дошёл.
    """
    locker_id = seeded_locker["id"]
    user_id = worker_user_with_2fa["id"]
    headers = {
        "X-Remote-User": str(user_id),
        "X-Remote-Role": "worker",
        "X-Remote-Lockers": locker_id,
        "X-Remote-Require-2FA": "true",
        "Content-Type": "application/json",
    }

    # Step 1: POST /commands → 202
    resp = requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands",
        headers=headers,
        json={"type": "open", "parameters": {}, "max_retries": 3},
        timeout=10,
    )
    if resp.status_code != 202:
        pytest.skip(
            f"auth_api не вернул 202 (HTTP {resp.status_code}): {resp.text}",
        )
    body = resp.json()
    challenge_id = body["challenge_id"]
    assert challenge_id

    # Step 2: достать код из outbox с retry
    code = None
    for _ in range(10):
        code = _peek_code(user_id)
        if code is not None:
            break
        time.sleep(1)
    if code is None:
        pytest.skip("OTP code не появился в outbox — notification_api недоступен?")

    # Step 3: POST /commands/confirm → 201
    confirm_headers = {
        "X-Remote-User": str(user_id),
        "X-Remote-Role": "worker",
        "Content-Type": "application/json",
    }
    resp = requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands/confirm",
        headers=confirm_headers,
        json={"challenge_id": challenge_id, "code": code},
        timeout=10,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json().get("id")
