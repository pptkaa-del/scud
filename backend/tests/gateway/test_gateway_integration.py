"""
Интеграционные тесты через Apache Gateway.

Все запросы идут через http://apache_gateway (порт 80).
Аутентификация — cookie access_token (устанавливается при /login).
Apache Lua проверяет cookie, ставит X-Remote-User перед проксированием.
"""
import requests

from conftest import GATEWAY_URL, ADMIN_PHONE, ADMIN_PASSWORD, unique_phone


# =============================================================================
# Аутентификация
# =============================================================================

def test_login_returns_cookie(wait_for_gateway):
    """POST /api/v1/auth/login → 200 + access_token cookie."""
    session = requests.Session()
    resp = session.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
    )
    assert resp.status_code == 200
    assert resp.json().get("message") == "Authentication successful"
    assert "access_token" in session.cookies


def test_login_invalid_credentials_returns_401(wait_for_gateway):
    """Неверный пароль → 401."""
    resp = requests.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": ADMIN_PHONE, "password": "definitely_wrong_password"},
    )
    assert resp.status_code == 401


def test_request_without_cookie_returns_401(wait_for_gateway):
    """Защищённый эндпоинт без cookie → 401 от Apache Lua."""
    resp = requests.get(f"{GATEWAY_URL}/api/v1/users")
    assert resp.status_code == 401


def test_logout_clears_session(wait_for_gateway):
    """
    Logout → 204, после чего токен не работает.
    Создаёт отдельную сессию чтобы не трогать shared admin_session.
    """
    # Логинимся отдельной сессией специально для этого теста
    session = requests.Session()
    login_resp = session.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
    )
    assert login_resp.status_code == 200, (
        f"Login failed (rate limit?): {login_resp.status_code} {login_resp.text}"
    )
    token = session.cookies.get("access_token")
    assert token

    logout_resp = session.post(f"{GATEWAY_URL}/api/v1/auth/logout")
    assert logout_resp.status_code == 204

    # Токен после logout → 401
    stale = requests.Session()
    stale.cookies.set("access_token", token)
    resp = stale.get(f"{GATEWAY_URL}/api/v1/users")
    assert resp.status_code == 401


# =============================================================================
# Пользователи с кириллическими именами
# =============================================================================

def test_create_user_with_cyrillic_name(admin_session):
    """Создание пользователя с полным именем на кириллице → 201."""
    phone = unique_phone()
    full_name = "Иванов Иван Иванович"

    resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": phone, "full_name": full_name, "password": "securepass1"},
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"

    user = resp.json()
    assert user["full_name"] == full_name, (
        f"Cyrillic name mangled: expected '{full_name}', got '{user['full_name']}'"
    )
    assert user["phone"] == phone
    assert user["state"] == "active"

    # Cleanup
    admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user['id']}")


def test_get_user_preserves_cyrillic_name(admin_session):
    """GET /api/v1/users/{id} возвращает кириллическое имя без изменений."""
    phone = unique_phone()
    full_name = "Петрова Мария Сергеевна"

    create_resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": phone, "full_name": full_name, "password": "securepass1"},
    )
    assert create_resp.status_code == 201
    user_id = create_resp.json()["id"]

    get_resp = admin_session.get(f"{GATEWAY_URL}/api/v1/user/{user_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["full_name"] == full_name

    # Cleanup
    admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}")


def test_user_list_contains_cyrillic_user(admin_session):
    """GET /api/v1/users возвращает список, в котором есть созданный пользователь."""
    phone = unique_phone()
    full_name = "Сидоров Алексей Владимирович"

    create_resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": phone, "full_name": full_name, "password": "securepass1"},
    )
    assert create_resp.status_code == 201
    user_id = create_resp.json()["id"]

    list_resp = admin_session.get(f"{GATEWAY_URL}/api/v1/users")
    assert list_resp.status_code == 200

    users = list_resp.json()["items"]
    assert isinstance(users, list)
    found = next((u for u in users if u["id"] == user_id), None)
    assert found is not None, "Created user not found in list"
    assert found["full_name"] == full_name

    # Cleanup
    admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}")


def test_create_user_with_mixed_cyrillic_latin_name(admin_session):
    """Имя из кириллицы и латиницы сохраняется корректно."""
    phone = unique_phone()
    full_name = "Müller Кириллов Jean"

    resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": phone, "full_name": full_name, "password": "securepass1"},
    )
    assert resp.status_code == 201, f"Create user failed: {resp.text}"

    user = resp.json()
    assert user["full_name"] == full_name

    # Cleanup
    admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user['id']}")


def test_update_user_with_cyrillic_name(admin_session):
    """Обновление full_name на кириллицу через PUT/PATCH (если эндпоинт есть)."""
    phone = unique_phone()
    original_name = "Test User Original"
    updated_name = "Обновлённый Пользователь"

    create_resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": phone, "full_name": original_name, "password": "securepass1"},
    )
    assert create_resp.status_code == 201
    user_id = create_resp.json()["id"]

    patch_resp = admin_session.patch(
        f"{GATEWAY_URL}/api/v1/user/{user_id}",
        json={"full_name": updated_name},
    )
    # PATCH /api/v1/user/{id} реализован в UserHandler.patch (user_api/app/handlers/user.py)
    # и проксируется gateway'ем (gateway/apache.conf, ProxyPass /api/v1/user/); ACM
    # разрешает его для Role.OPERATOR+ (acm/app/rules.py), а admin_session — administrator.
    # full_name — непривилегированное поле, поэтому обновление проходит без 403 и
    # завершается write_json(updated) со статусом 200.
    assert patch_resp.status_code == 200, f"Unexpected PATCH status: {patch_resp.text}"
    assert patch_resp.json()["full_name"] == updated_name

    # Cleanup
    admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}")


# =============================================================================
# Жизненный цикл пользователя
# =============================================================================

def test_full_user_lifecycle(admin_session):
    """Полный цикл: создание → чтение → удаление через gateway."""
    phone = unique_phone()
    full_name = "Николаев Дмитрий Александрович"

    # Создание
    create_resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": phone, "full_name": full_name, "password": "securepass1"},
    )
    assert create_resp.status_code == 201
    user = create_resp.json()
    user_id = user["id"]
    assert user["full_name"] == full_name
    assert user["phone"] == phone
    assert user["state"] == "active"
    assert user["role"] == "worker"

    # Чтение
    get_resp = admin_session.get(f"{GATEWAY_URL}/api/v1/user/{user_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["full_name"] == full_name

    # Удаление
    delete_resp = admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}")
    assert delete_resp.status_code == 204

    # После удаления — пользователь недоступен или деактивирован
    get_after_resp = admin_session.get(f"{GATEWAY_URL}/api/v1/user/{user_id}")
    assert get_after_resp.status_code in (404, 200)
    if get_after_resp.status_code == 200:
        assert get_after_resp.json()["state"] in ("deleted", "deactivated")


def test_create_user_duplicate_phone_returns_409(admin_session, created_user):
    """Дублирующий телефон → 409 Conflict."""
    resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": created_user["phone"], "full_name": "Другое Имя"},
    )
    assert resp.status_code == 409


def test_create_user_invalid_phone_returns_400(admin_session):
    """Невалидный формат телефона → 400."""
    resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": "not-a-phone", "full_name": "Тест Тестович"},
    )
    assert resp.status_code == 400
