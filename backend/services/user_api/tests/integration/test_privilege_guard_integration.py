"""Интеграционные тесты privilege guard (SEC-01 / SEC-03).

Проверяют, что operator не может эскалировать привилегии через реальный
стек user_api + БД: PATCH role/password и enable/disable-2fa на target'ах
>= своей роли отклоняются 403, а легитимные операции с worker'ами проходят.

Caller-роль guard берёт из БД (users.role загружается в prepare() по
X-Remote-User), поэтому тесты заводят реального operator'а и шлют запросы
от его id.
"""
import time

import psycopg2
import requests

from tests.integration.conftest import BASE_URL, DB_CONFIG


def _unique_phone() -> str:
    return f"+7{str(int(time.time() * 1000))[-10:]}"


def _seed_user(role: str) -> int:
    """Создать пользователя с заданной ролью напрямую в БД, вернуть id."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO users (phone, full_name, state, role)
                VALUES (%s, %s, 'active', %s)
                RETURNING id
                """,
                (_unique_phone(), f"Guard {role}", role),
            )
            user_id = cur.fetchone()[0]
            conn.commit()
            return int(user_id)
    finally:
        conn.close()


def _headers(user_id: int) -> dict:
    # X-Remote-Role намеренно НЕ шлём: guard читает роль из БД (current_user).
    return {"X-Remote-User": str(user_id)}


def test_operator_cannot_self_promote(wait_for_api):
    operator_id = _seed_user("operator")
    resp = requests.patch(
        f"{BASE_URL}/api/v1/user/{operator_id}",
        json={"role": "administrator"},
        headers=_headers(operator_id),
    )
    assert resp.status_code == 403
    # Роль в БД не изменилась.
    check = requests.get(
        f"{BASE_URL}/api/v1/user/{operator_id}", headers=_headers(operator_id)
    )
    assert check.json()["role"] == "operator"


def test_operator_cannot_set_admin_role_on_other(wait_for_api):
    operator_id = _seed_user("operator")
    worker_id = _seed_user("worker")
    resp = requests.patch(
        f"{BASE_URL}/api/v1/user/{worker_id}",
        json={"role": "administrator"},
        headers=_headers(operator_id),
    )
    assert resp.status_code == 403


def test_operator_cannot_reset_admin_password(wait_for_api):
    operator_id = _seed_user("operator")
    admin_id = _seed_user("administrator")
    resp = requests.patch(
        f"{BASE_URL}/api/v1/user/{admin_id}",
        json={"password": "brandnewpass123"},
        headers=_headers(operator_id),
    )
    assert resp.status_code == 403


def test_operator_cannot_disable_admin_2fa(wait_for_api):
    operator_id = _seed_user("operator")
    admin_id = _seed_user("administrator")
    resp = requests.post(
        f"{BASE_URL}/api/v1/user/{admin_id}/disable-2fa",
        headers=_headers(operator_id),
    )
    assert resp.status_code == 403


def test_operator_can_update_worker(wait_for_api):
    operator_id = _seed_user("operator")
    worker_id = _seed_user("worker")
    new_phone = _unique_phone()
    resp = requests.patch(
        f"{BASE_URL}/api/v1/user/{worker_id}",
        json={"phone": new_phone},
        headers=_headers(operator_id),
    )
    assert resp.status_code == 200
    assert resp.json()["phone"] == new_phone


def test_operator_cannot_create_administrator(wait_for_api):
    operator_id = _seed_user("operator")
    resp = requests.post(
        f"{BASE_URL}/api/v1/users",
        json={
            "phone": _unique_phone(),
            "full_name": "Should Not Exist",
            "role": "administrator",
        },
        headers=_headers(operator_id),
    )
    assert resp.status_code == 403


def test_administrator_can_promote_worker(wait_for_api):
    admin_id = _seed_user("administrator")
    worker_id = _seed_user("worker")
    resp = requests.patch(
        f"{BASE_URL}/api/v1/user/{worker_id}",
        json={"role": "operator"},
        headers=_headers(admin_id),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "operator"
