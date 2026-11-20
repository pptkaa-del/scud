"""Интеграционные тесты для API пользователей"""
import os
import time

import requests

from tests.integration.conftest import BASE_URL, REMOTE_USER_ID, TEST_PHONE


def generate_unique_phone():
    """Генерирует уникальный номер телефона на основе timestamp"""
    timestamp = str(int(time.time() * 1000))[-10:]  # Последние 10 цифр timestamp
    return f"+7{timestamp}"


def test_get_current_user(wait_for_api):
    """Проверка получения данных текущего (системного) пользователя"""
    url = f"{BASE_URL}/api/v1/user/{REMOTE_USER_ID}"
    headers = {"X-Remote-User": REMOTE_USER_ID}

    response = requests.get(url, headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == int(REMOTE_USER_ID)
    assert data["phone"] == TEST_PHONE


def test_create_user(api_client):
    """Проверка создания нового пользователя"""
    payload = {
        "phone": generate_unique_phone(),
        "full_name": "Integration Test User"
    }

    # 1. Создаем пользователя
    response = api_client.post("/api/v1/users", json=payload)
    assert response.status_code == 201
    new_user = response.json()
    assert new_user["phone"] == payload["phone"]
    user_id = new_user["id"]

    # 2. Проверяем существование через GET
    response = api_client.get(f"/api/v1/user/{user_id}")
    assert response.status_code == 200
    assert response.json()["full_name"] == payload["full_name"]

    # 3. Удаляем (деактивируем)
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204


def test_unauthorized_access(wait_for_api):
    """Проверка ошибки авторизации при отсутствии заголовка"""
    url = f"{BASE_URL}/api/v1/user/{REMOTE_USER_ID}"

    response = requests.get(url)  # Нет X-Remote-User
    assert response.status_code == 401
    data = response.json()
    assert data["title"] == "Unauthorized"
    assert data["status"] == 401
    assert "Missing X-Remote-User" in data["detail"]


def test_create_duplicate_phone(api_client):
    """Проверка ошибки при создании пользователя с дублирующимся телефоном"""
    payload = {
        "phone": "+79009998877",
        "full_name": "Duplicate User"
    }

    # Создаем первого пользователя
    api_client.post("/api/v1/users", json=payload)

    # Пробуем создать второго с тем же номером
    response = api_client.post("/api/v1/users", json=payload)
    assert response.status_code == 409


def test_create_user_invalid_phone_format(api_client):
    """Проверка валидации формата телефона"""
    test_cases = [
        {"phone": "79001234567", "full_name": "Test User"},  # без знака +
        {"phone": "+89001234567", "full_name": "Test User"},  # неправильный код страны
        {"phone": "+7900123456", "full_name": "Test User"},  # слишком короткий номер
        {"phone": "+790012345678", "full_name": "Test User"},  # слишком длинный номер
        {"phone": "+7900abcd567", "full_name": "Test User"}  # буквы в номере
    ]

    for payload in test_cases:
        response = api_client.post("/api/v1/users", json=payload)
        assert response.status_code == 400
        assert "phone" in response.json()["detail"]


def test_get_users_list(api_client):
    """Проверка получения списка пользователей"""
    response = api_client.get("/api/v1/users")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)
    assert data["total"] > 0  # Должен быть хотя бы системный пользователь
    assert data["limit"] == 100
    assert data["offset"] == 0


def test_get_users_list_with_filters(api_client):
    """Проверка фильтрации пользователей по state и contractor_id"""
    response = api_client.get("/api/v1/users?state=active")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)
    # Все пользователи должны быть active
    for user in data["items"]:
        assert user["state"] == "active"


def test_user_contractor_relationship(api_client):
    """Проверка связи пользователь-подрядчик и contractor_name"""
    # 1. Создаем подрядчика
    contractor_payload = {"name": "Test Contractor for User"}
    response = api_client.post("/api/v1/contractors", json=contractor_payload)
    assert response.status_code == 201
    contractor_id = response.json()["id"]
    contractor_name = response.json()["name"]

    # 2. Создаем пользователя с привязкой к подрядчику
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "User with Contractor",
        "contractor_id": contractor_id
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user = response.json()
    assert user["contractor_id"] == contractor_id
    assert user["contractor_name"] == contractor_name  # Проверяем contractor_name через JOIN
    user_id = user["id"]

    # 3. Получаем пользователя по ID и проверяем contractor_name
    response = api_client.get(f"/api/v1/user/{user_id}")
    assert response.status_code == 200
    user_data = response.json()
    assert user_data["contractor_name"] == contractor_name

    # 4. Получаем список пользователей по contractor_id
    response = api_client.get(f"/api/v1/users?contractor_id={contractor_id}")
    assert response.status_code == 200
    users = response.json()["items"]
    assert len(users) > 0
    assert any(u["id"] == user_id for u in users)
    # Проверяем, что contractor_name присутствует в списке
    matching_user = next((u for u in users if u["id"] == user_id), None)
    assert matching_user is not None
    assert matching_user["contractor_name"] == contractor_name

    # 5. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204

    # 6. Удаляем подрядчика
    response = api_client.delete(f"/api/v1/contractor/{contractor_id}")
    assert response.status_code == 204


def test_create_user_with_password(api_client, db_connection):
    """Пользователь с паролем: user_auth запись создаётся с bcrypt-хешем."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users",
                           json={"phone": phone, "full_name": "Тест Пароль", "password": "mypassword1"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    with db_connection.cursor() as cur:
        cur.execute("SELECT password_hash, require_2fa FROM user_auth WHERE user_id = %s", (user_id,))
        row = cur.fetchone()

    assert row is not None, "user_auth запись должна существовать"
    assert row[0].startswith("$2b$"), "password_hash должен быть bcrypt хешем"
    assert row[1] is True, "require_2fa должен быть True по умолчанию для новых пользователей"


def test_patch_password_preserves_require_2fa(api_client, db_connection):
    """Смена пароля через PATCH не сбрасывает require_2fa."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users",
                           json={"phone": phone, "full_name": "Тест 2FA", "password": "mypassword1"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]  # создан с require_2fa=True по умолчанию

    resp = api_client.patch(f"/api/v1/user/{user_id}", json={"password": "newpassword2"})
    assert resp.status_code == 200

    with db_connection.cursor() as cur:
        cur.execute("SELECT require_2fa FROM user_auth WHERE user_id = %s", (user_id,))
        assert cur.fetchone()[0] is True, "смена пароля не должна сбрасывать 2FA"


def test_create_user_without_password(api_client, db_connection):
    """Пользователь без пароля: user_auth запись не создаётся."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users",
                           json={"phone": phone, "full_name": "Тест Без Пароля"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    with db_connection.cursor() as cur:
        cur.execute("SELECT user_id FROM user_auth WHERE user_id = %s", (user_id,))
        row = cur.fetchone()

    assert row is None, "user_auth запись не должна существовать"


def test_create_user_with_short_password_returns_400(api_client):
    """Пароль короче 8 символов → 400 Bad Request."""
    resp = api_client.post("/api/v1/users",
                           json={"phone": generate_unique_phone(), "full_name": "Тест", "password": "short"})
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# require_2fa в GET /user/{id} и GET /users (LEFT JOIN user_auth)
# ---------------------------------------------------------------------------

def _find_in_list(api_client, suffix, user_id):
    """Найти пользователя в списке /api/v1/users по уникальному суффиксу имени."""
    resp = api_client.get(f"/api/v1/users?q={suffix}")
    assert resp.status_code == 200
    items = resp.json()["items"]
    match = next((u for u in items if u["id"] == user_id), None)
    assert match is not None, f"пользователь {user_id} должен быть в списке (q={suffix})"
    return match


def test_user_created_with_password_has_require_2fa_true(api_client):
    """Пользователь, созданный с паролем: require_2fa=True в create-ответе, GET и списке.

    Новый пользователь с паролем получает 2FA по умолчанию, и все три пути
    (ответ на POST, GET /user/{id}, элемент GET /users) должны это отражать
    согласованно.
    """
    phone = generate_unique_phone()
    time.sleep(0.002)
    suffix = str(int(time.time() * 1000))[-6:]
    full_name = f"2FA Status True {suffix}"

    resp = api_client.post("/api/v1/users",
                           json={"phone": phone, "full_name": full_name, "password": "mypassword1"})
    assert resp.status_code == 201
    created = resp.json()
    user_id = created["id"]
    assert created["require_2fa"] is True

    resp = api_client.get(f"/api/v1/user/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["require_2fa"] is True

    match = _find_in_list(api_client, suffix, user_id)
    assert match["require_2fa"] is True

    api_client.delete(f"/api/v1/user/{user_id}")


def test_user_with_2fa_disabled_has_require_2fa_false(api_client):
    """После POST /user/{id}/disable-2fa: require_2fa=False в GET и списке."""
    phone = generate_unique_phone()
    time.sleep(0.002)
    suffix = str(int(time.time() * 1000))[-6:]
    full_name = f"2FA Status Disabled {suffix}"

    resp = api_client.post("/api/v1/users",
                           json={"phone": phone, "full_name": full_name, "password": "mypassword1"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    resp = api_client.post(f"/api/v1/user/{user_id}/disable-2fa", json={})
    assert resp.status_code == 204

    resp = api_client.get(f"/api/v1/user/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["require_2fa"] is False

    match = _find_in_list(api_client, suffix, user_id)
    assert match["require_2fa"] is False

    api_client.delete(f"/api/v1/user/{user_id}")


def test_user_without_password_has_require_2fa_false(api_client):
    """Пользователь без пароля (нет строки user_auth): require_2fa=False через COALESCE."""
    phone = generate_unique_phone()
    time.sleep(0.002)
    suffix = str(int(time.time() * 1000))[-6:]
    full_name = f"2FA Status NoAuth {suffix}"

    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": full_name})
    assert resp.status_code == 201
    created = resp.json()
    user_id = created["id"]
    assert created["require_2fa"] is False

    resp = api_client.get(f"/api/v1/user/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["require_2fa"] is False

    match = _find_in_list(api_client, suffix, user_id)
    assert match["require_2fa"] is False

    api_client.delete(f"/api/v1/user/{user_id}")


# ---------------------------------------------------------------------------
# PATCH /api/v1/user/{id}
# ---------------------------------------------------------------------------

def test_patch_user_update_full_name(api_client):
    """Обновление имени пользователя."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Original Name"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    resp = api_client.patch(f"/api/v1/user/{user_id}", json={"full_name": "Updated Name"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["full_name"] == "Updated Name"
    assert data["phone"] == phone  # телефон не изменился


def test_patch_user_update_phone(api_client):
    """Обновление телефона — новый телефон свободен."""
    old_phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": old_phone, "full_name": "Phone Test"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    new_phone = generate_unique_phone()
    resp = api_client.patch(f"/api/v1/user/{user_id}", json={"phone": new_phone})
    assert resp.status_code == 200
    assert resp.json()["phone"] == new_phone


def test_patch_user_phone_conflict(api_client):
    """Телефон занят другим пользователем → 409."""
    phone_a = generate_unique_phone()
    time.sleep(0.002)  # generate_unique_phone uses ms-timestamp — ensure uniqueness
    phone_b = generate_unique_phone()
    api_client.post("/api/v1/users", json={"phone": phone_a, "full_name": "User A"})
    resp_b = api_client.post("/api/v1/users", json={"phone": phone_b, "full_name": "User B"})
    user_b_id = resp_b.json()["id"]

    resp = api_client.patch(f"/api/v1/user/{user_b_id}", json={"phone": phone_a})
    assert resp.status_code == 409
    assert resp.json()["title"] == "Conflict"


def test_patch_user_not_found(api_client):
    """Обновление несуществующего пользователя → 404."""
    resp = api_client.patch("/api/v1/user/999999999", json={"full_name": "Ghost"})
    assert resp.status_code == 404


def test_patch_user_empty_body(api_client):
    """Пустое тело запроса → 400."""
    resp = api_client.patch(f"/api/v1/user/{REMOTE_USER_ID}", json={})
    assert resp.status_code == 400


def test_patch_user_invalid_phone(api_client):
    """Невалидный формат телефона → 400."""
    resp = api_client.patch(f"/api/v1/user/{REMOTE_USER_ID}", json={"phone": "not-a-phone"})
    assert resp.status_code == 400
    assert "phone" in resp.json()["detail"]


def test_patch_user_update_role(api_client):
    """Обновление роли пользователя."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Role Test"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]
    assert resp.json()["role"] == "worker"

    resp = api_client.patch(f"/api/v1/user/{user_id}", json={"role": "operator"})
    assert resp.status_code == 200
    assert resp.json()["role"] == "operator"


def test_patch_user_update_password(api_client, db_connection):
    """Обновление пароля — hash в user_auth обновляется."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users",
                           json={"phone": phone, "full_name": "Pass Update", "password": "oldpassword1"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    with db_connection.cursor() as cur:
        cur.execute("SELECT password_hash FROM user_auth WHERE user_id = %s", (user_id,))
        old_hash = cur.fetchone()[0]

    resp = api_client.patch(f"/api/v1/user/{user_id}", json={"password": "newpassword1"})
    assert resp.status_code == 200

    with db_connection.cursor() as cur:
        cur.execute("SELECT password_hash FROM user_auth WHERE user_id = %s", (user_id,))
        new_hash = cur.fetchone()[0]

    assert new_hash != old_hash
    assert new_hash.startswith("$2b$")


def test_patch_user_short_password(api_client):
    """Пароль короче 8 символов при обновлении → 400."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Short Pass"})
    user_id = resp.json()["id"]

    resp = api_client.patch(f"/api/v1/user/{user_id}", json={"password": "short"})
    assert resp.status_code == 400


def test_patch_user_multiple_fields(api_client):
    """Обновление нескольких полей одновременно."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Multi Test"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    new_phone = generate_unique_phone()
    resp = api_client.patch(f"/api/v1/user/{user_id}",
                            json={"full_name": "Multi Updated", "phone": new_phone, "role": "operator"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["full_name"] == "Multi Updated"
    assert data["phone"] == new_phone
    assert data["role"] == "operator"


def test_patch_user_same_phone_succeeds(api_client):
    """Обновление с тем же телефоном что и сейчас — конфликта нет."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Same Phone"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    resp = api_client.patch(f"/api/v1/user/{user_id}",
                            json={"phone": phone, "full_name": "Same Phone Updated"})
    assert resp.status_code == 200
    assert resp.json()["full_name"] == "Same Phone Updated"


def test_patch_user_unauthorized(wait_for_api):
    """Отсутствует X-Remote-User → 401."""
    import requests as req
    resp = req.patch(f"{BASE_URL}/api/v1/user/1", json={"full_name": "Ghost"})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/user/{id}/reactivate
# ---------------------------------------------------------------------------

def test_deactivate_and_reactivate_user(api_client):
    """Деактивация и реактивация пользователя."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Reactivate Test"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    # Деактивируем
    resp = api_client.delete(f"/api/v1/user/{user_id}")
    assert resp.status_code == 204

    # Проверяем state=deactivated
    resp = api_client.get(f"/api/v1/user/{user_id}")
    assert resp.status_code == 200
    assert resp.json()["state"] == "deactivated"

    # Реактивируем
    resp = api_client.post(f"/api/v1/user/{user_id}/reactivate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["state"] == "active"
    assert data["id"] == user_id

    # Idempotent: повторная реактивация не падает
    resp = api_client.post(f"/api/v1/user/{user_id}/reactivate")
    assert resp.status_code == 200
    assert resp.json()["state"] == "active"

    # Cleanup
    api_client.delete(f"/api/v1/user/{user_id}")


def test_soft_deleted_user_excluded_from_get_and_list(api_client, db_connection):
    """Soft-delete marker state='deleted' исключает пользователя из GET и list.

    `deactivated` остаётся видимым (см. test_deactivate_and_reactivate_user),
    а вот терминальный `deleted` репозиторий фильтрует
    (`get_by_id`/`list` имеют `state <> 'deleted'` без include_deleted).
    Перевод в 'deleted' делаем напрямую в БД (через API недоступен — soft-delete
    по DELETE ставит 'deactivated'), затем проверяем 404 на GET и отсутствие
    в списке.
    """
    phone = generate_unique_phone()
    resp = api_client.post(
        "/api/v1/users", json={"phone": phone, "full_name": "Soft Deleted"}
    )
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    # До удаления виден.
    assert api_client.get(f"/api/v1/user/{user_id}").status_code == 200

    # Помечаем терминальным soft-delete маркером напрямую в БД.
    with db_connection.cursor() as cur:
        cur.execute(
            "UPDATE users SET state = 'deleted' WHERE id = %s", (user_id,)
        )
    db_connection.commit()

    # GET /user/{id} → 404 (репозиторий исключает deleted).
    assert api_client.get(f"/api/v1/user/{user_id}").status_code == 404

    # И в общем списке его тоже нет.
    resp = api_client.get(f"/api/v1/users?q={_urlsafe_name('Soft Deleted')}")
    assert resp.status_code == 200
    ids = [u["id"] for u in resp.json().get("items", [])]
    assert user_id not in ids, "soft-deleted user must not appear in list"


def _urlsafe_name(name: str) -> str:
    from urllib.parse import quote

    return quote(name)


def test_reactivate_nonexistent_user(api_client):
    """Реактивация несуществующего пользователя → 404."""
    resp = api_client.post("/api/v1/user/999999999/reactivate")
    assert resp.status_code == 404


def test_reactivate_user_unauthorized(wait_for_api):
    """Отсутствует X-Remote-User → 401."""
    import requests as req
    resp = req.post(f"{BASE_URL}/api/v1/user/1/reactivate")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/v1/users?q=
# ---------------------------------------------------------------------------

def test_search_users_by_name(api_client):
    """Поиск по имени возвращает только совпадающих пользователей."""
    unique_suffix = str(int(time.time() * 1000))[-6:]
    name = f"Поиск Тестов {unique_suffix}"
    phone = generate_unique_phone()

    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": name})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    resp = api_client.get(f"/api/v1/users?q={unique_suffix}")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data["items"], list)
    assert any(u["id"] == user_id for u in data["items"])
    assert data["total"] >= 1

    # Cleanup
    api_client.delete(f"/api/v1/user/{user_id}")


def test_search_users_case_insensitive(api_client):
    """Поиск нечувствителен к регистру."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Регистр Тест Регистров"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    for query in ["регистр", "РЕГИСТР", "Регистр"]:
        resp = api_client.get(f"/api/v1/users?q={query}")
        assert resp.status_code == 200
        assert any(u["id"] == user_id for u in resp.json()["items"]), f"Not found with q={query!r}"

    # Cleanup
    api_client.delete(f"/api/v1/user/{user_id}")


def test_search_users_no_results(api_client):
    """Поиск по несуществующему имени возвращает пустой список."""
    resp = api_client.get("/api/v1/users?q=xyzzy_nobody_12345")
    assert resp.status_code == 200
    data = resp.json()
    assert data["items"] == []
    assert data["total"] == 0


def test_search_users_empty_string_ignored(api_client):
    """Пустой q= игнорируется и возвращает всех пользователей."""
    resp_all = api_client.get("/api/v1/users")
    resp_empty = api_client.get("/api/v1/users?q=")
    assert resp_all.status_code == 200
    assert resp_empty.status_code == 200
    assert resp_all.json()["total"] == resp_empty.json()["total"]


def test_search_combined_with_filters(api_client):
    """q комбинируется с другими фильтрами."""
    phone = generate_unique_phone()
    resp = api_client.post("/api/v1/users", json={"phone": phone, "full_name": "Комбо Фильтров Тест"})
    assert resp.status_code == 201
    user_id = resp.json()["id"]

    # С фильтром state=active — пользователь должен найтись
    resp = api_client.get("/api/v1/users?q=Комбо&state=active")
    assert resp.status_code == 200
    assert any(u["id"] == user_id for u in resp.json()["items"])

    # С фильтром state=deactivated — пользователь не должен найтись
    resp = api_client.get("/api/v1/users?q=Комбо&state=deactivated")
    assert resp.status_code == 200
    assert not any(u["id"] == user_id for u in resp.json()["items"])

    # Cleanup
    api_client.delete(f"/api/v1/user/{user_id}")
