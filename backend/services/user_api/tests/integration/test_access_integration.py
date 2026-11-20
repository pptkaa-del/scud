"""Интеграционные тесты для API управления допусками пользователей"""
from datetime import datetime
import time

from tests.integration.conftest import BASE_URL, REMOTE_USER_ID


def generate_unique_phone():
    """Генерирует уникальный номер телефона на основе timestamp"""
    timestamp = str(int(time.time() * 1000))[-10:]
    return f"+7{timestamp}"


def test_create_access_full_access(api_client):
    """Проверка создания допуска с полным доступом ко всем шкафам"""
    # 1. Создаем пользователя для теста
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "Access Test User Full"
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user_id = response.json()["id"]

    # 2. Создаем допуск с full_access=true
    access_payload = {
        "start_at": "2026-03-29T00:00:00Z",
        "end_at": "2026-12-31T23:59:59Z",
        "full_access": True
    }
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json=access_payload)
    assert response.status_code == 201
    access = response.json()
    assert access["user_id"] == user_id
    assert access["full_access"] is True
    assert access["is_active"] is True
    assert access["lockers"] == []  # Пустой список для full_access
    assert "created_at" in access
    access_id = access["id"]

    # 3. Получаем допуск по ID
    response = api_client.get(f"/api/v1/access/{access_id}")
    assert response.status_code == 200
    access_data = response.json()
    assert access_data["id"] == access_id
    assert access_data["full_access"] is True

    # 4. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204


def test_create_access_with_specific_lockers(api_client):
    """Проверка создания допуска с указанием конкретных шкафов"""
    # 1. Создаем пользователя для теста
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "Access Test User Limited"
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user_id = response.json()["id"]

    locker_uuids = [
        "550e8400-e29b-41d4-a716-446655440001",
        "550e8400-e29b-41d4-a716-446655440002",
        "550e8400-e29b-41d4-a716-446655440003",
    ]

    # 2. Создаем допуск с указанием конкретных шкафов
    access_payload = {
        "start_at": "2026-03-29T00:00:00Z",
        "end_at": "2026-12-31T23:59:59Z",
        "full_access": False,
        "locker_ids": locker_uuids
    }
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json=access_payload)
    assert response.status_code == 201
    access = response.json()
    assert access["user_id"] == user_id
    assert access["full_access"] is False
    assert sorted(access["lockers"]) == sorted(locker_uuids)
    assert access["is_active"] is True
    access_id = access["id"]

    # 3. Проверяем допуск через GET
    response = api_client.get(f"/api/v1/access/{access_id}")
    assert response.status_code == 200
    access_data = response.json()
    assert sorted(access_data["lockers"]) == sorted(locker_uuids)

    # 4. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204


def test_create_access_validation_errors(api_client):
    """Проверка валидации при создании допуска"""
    # 1. Создаем пользователя для теста
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "Validation Test User"
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user_id = response.json()["id"]

    # 2. Проверяем ошибку: отсутствует start_at
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json={
        "full_access": True
    })
    assert response.status_code == 400
    assert "start_at" in response.json()["detail"]

    # 3. Проверяем ошибку: отсутствует full_access
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json={
        "start_at": "2026-03-29T00:00:00Z"
    })
    assert response.status_code == 400
    assert "full_access" in response.json()["detail"]

    # 4. Проверяем ошибку: full_access=false но нет locker_ids
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json={
        "start_at": "2026-03-29T00:00:00Z",
        "full_access": False
    })
    assert response.status_code == 400
    assert "locker_ids" in response.json()["detail"]

    # 5. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204


def test_get_user_accesses_list(api_client):
    """Проверка получения списка допусков пользователя"""
    # 1. Создаем пользователя
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "List Test User"
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user_id = response.json()["id"]

    # 2. Создаем несколько допусков
    access_payload_1 = {
        "start_at": "2026-03-29T00:00:00Z",
        "end_at": "2026-06-30T23:59:59Z",
        "full_access": True
    }
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json=access_payload_1)
    assert response.status_code == 201

    list_locker_uuids = [
        "550e8400-e29b-41d4-a716-446655440005",
        "550e8400-e29b-41d4-a716-446655440006",
    ]
    access_payload_2 = {
        "start_at": "2026-07-01T00:00:00Z",
        "full_access": False,
        "locker_ids": list_locker_uuids
    }
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json=access_payload_2)
    assert response.status_code == 201

    # 3. Получаем список допусков
    response = api_client.get(f"/api/v1/user/{user_id}/accesses")
    assert response.status_code == 200
    accesses = response.json()["items"]
    assert len(accesses) >= 2
    # Проверяем, что оба допуска присутствуют
    assert any(a["full_access"] is True for a in accesses)
    assert any(a["full_access"] is False and sorted(a["lockers"]) == sorted(list_locker_uuids) for a in accesses)

    # 4. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204


def test_update_access(api_client):
    """Проверка обновления допуска"""
    # 1. Создаем пользователя и допуск
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "Update Test User"
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user_id = response.json()["id"]

    update_locker_uuids = [
        "550e8400-e29b-41d4-a716-446655440007",
        "550e8400-e29b-41d4-a716-446655440008",
    ]
    access_payload = {
        "start_at": "2026-03-29T00:00:00Z",
        "end_at": "2026-06-30T23:59:59Z",
        "full_access": False,
        "locker_ids": update_locker_uuids
    }
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json=access_payload)
    assert response.status_code == 201
    access_id = response.json()["id"]

    # 2. Обновляем end_at
    update_payload = {
        "end_at": "2026-12-31T23:59:59Z"
    }
    response = api_client.patch(f"/api/v1/access/{access_id}", json=update_payload)
    assert response.status_code == 200
    updated_access = response.json()
    expected_date = datetime.fromisoformat("2026-12-31T23:59:59Z")
    actual_date = datetime.fromisoformat(updated_access["end_at"])
    assert expected_date == actual_date

    # 3. Обновляем список шкафов
    new_locker_uuids = [
        "550e8400-e29b-41d4-a716-446655440007",
        "550e8400-e29b-41d4-a716-446655440008",
        "550e8400-e29b-41d4-a716-446655440009",
        "550e8400-e29b-41d4-a716-44665544000a",
    ]
    update_payload = {
        "locker_ids": new_locker_uuids
    }
    response = api_client.patch(f"/api/v1/access/{access_id}", json=update_payload)
    assert response.status_code == 200
    updated_access = response.json()
    assert sorted(updated_access["lockers"]) == sorted(new_locker_uuids)

    # 4. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204


def test_deactivate_and_reactivate_access(api_client):
    """Проверка деактивации и реактивации допуска"""
    # 1. Создаем пользователя и допуск
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "Deactivate Test User"
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user_id = response.json()["id"]

    access_payload = {
        "start_at": "2026-03-29T00:00:00Z",
        "full_access": True
    }
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json=access_payload)
    assert response.status_code == 201
    access_id = response.json()["id"]

    # 2. Деактивируем допуск
    response = api_client.delete(f"/api/v1/access/{access_id}")
    assert response.status_code == 204

    # 3. Проверяем, что допуск деактивирован
    response = api_client.get(f"/api/v1/access/{access_id}")
    assert response.status_code == 200
    access_data = response.json()
    assert access_data["is_active"] is False

    # 4. Проверяем, что по умолчанию деактивированный допуск не показывается в списке
    response = api_client.get(f"/api/v1/user/{user_id}/accesses")
    assert response.status_code == 200
    accesses = response.json()["items"]
    assert not any(a["id"] == access_id for a in accesses)

    # 5. Проверяем, что с include_inactive=true деактивированный допуск показывается
    response = api_client.get(f"/api/v1/user/{user_id}/accesses?include_inactive=true")
    assert response.status_code == 200
    accesses = response.json()["items"]
    assert any(a["id"] == access_id and a["is_active"] is False for a in accesses)

    # 6. Реактивируем допуск
    response = api_client.post(f"/api/v1/access/{access_id}/reactivate")
    assert response.status_code == 200
    reactivated_access = response.json()
    assert reactivated_access["is_active"] is True

    # 7. Проверяем, что реактивированный допуск снова показывается в списке
    response = api_client.get(f"/api/v1/user/{user_id}/accesses")
    assert response.status_code == 200
    accesses = response.json()["items"]
    assert any(a["id"] == access_id and a["is_active"] is True for a in accesses)

    # 8. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204


def test_access_not_found(api_client):
    """Проверка ошибки 404 для несуществующего допуска"""
    response = api_client.get("/api/v1/access/999999")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_access_for_nonexistent_user(api_client):
    """Проверка ошибки при создании допуска для несуществующего пользователя"""
    access_payload = {
        "start_at": "2026-03-29T00:00:00Z",
        "full_access": True
    }
    response = api_client.post("/api/v1/user/999999/accesses", json=access_payload)
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


def test_batch_accesses(api_client):
    """Батч-запрос допусков для нескольких пользователей"""
    # 1. Создаём двух пользователей
    user_a_resp = api_client.post("/api/v1/users", json={
        "phone": generate_unique_phone(), "full_name": "Batch User A",
    })
    assert user_a_resp.status_code == 201
    user_a_id = user_a_resp.json()["id"]

    user_b_resp = api_client.post("/api/v1/users", json={
        "phone": generate_unique_phone(), "full_name": "Batch User B",
    })
    assert user_b_resp.status_code == 201
    user_b_id = user_b_resp.json()["id"]

    # 2. Даём допуск только пользователю A
    access_resp = api_client.post(f"/api/v1/user/{user_a_id}/accesses", json={
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": "2026-12-31T23:59:59Z",
        "full_access": True,
    })
    assert access_resp.status_code == 201
    access_id = access_resp.json()["id"]

    # 3. Батч-запрос: оба пользователя
    resp = api_client.post("/api/v1/accesses/batch", json={"user_ids": [user_a_id, user_b_id]})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert str(user_a_id) in items
    assert str(user_b_id) in items
    assert len(items[str(user_a_id)]) == 1
    assert items[str(user_a_id)][0]["id"] == access_id
    assert items[str(user_b_id)] == []

    # 4. Деактивируем допуск A
    resp_del = api_client.delete(f"/api/v1/access/{access_id}")
    assert resp_del.status_code == 204

    # 5. Без include_inactive — A возвращает пустой список
    resp = api_client.post("/api/v1/accesses/batch", json={"user_ids": [user_a_id]})
    assert resp.status_code == 200
    assert resp.json()["items"][str(user_a_id)] == []

    # 6. С include_inactive=true — деактивированный допуск виден
    resp = api_client.post("/api/v1/accesses/batch", json={
        "user_ids": [user_a_id], "include_inactive": True,
    })
    assert resp.status_code == 200
    a_accesses = resp.json()["items"][str(user_a_id)]
    assert len(a_accesses) == 1
    assert a_accesses[0]["is_active"] is False

    # 7. Несуществующий user_id → пустой список, не ошибка
    resp = api_client.post("/api/v1/accesses/batch", json={"user_ids": [999999]})
    assert resp.status_code == 200
    assert resp.json()["items"]["999999"] == []

    # Cleanup
    api_client.delete(f"/api/v1/user/{user_a_id}")
    api_client.delete(f"/api/v1/user/{user_b_id}")


def test_batch_accesses_validation(api_client):
    """Валидация батч-запроса допусков"""
    # Пустой user_ids → 400
    resp = api_client.post("/api/v1/accesses/batch", json={"user_ids": []})
    assert resp.status_code == 400
    assert "user_ids" in resp.json()["detail"]

    # Более 100 user_ids → 400
    resp = api_client.post("/api/v1/accesses/batch", json={"user_ids": list(range(1, 102))})
    assert resp.status_code == 400
    assert "100" in resp.json()["detail"]

    # Отсутствует поле user_ids → 400
    resp = api_client.post("/api/v1/accesses/batch", json={"include_inactive": False})
    assert resp.status_code == 400


def test_user_expire_date_updates_with_accesses(api_client):
    """expire_date пользователя обновляется при изменении допусков"""
    # 1. Создаём пользователя — expire_date должен быть null
    user_resp = api_client.post("/api/v1/users", json={
        "phone": generate_unique_phone(),
        "full_name": "Expire Date Test User",
    })
    assert user_resp.status_code == 201
    user_id = user_resp.json()["id"]
    assert api_client.get(f"/api/v1/user/{user_id}").json()["expire_date"] is None

    # 2. Создаём первый допуск с end_at
    end_at_1 = "2026-06-30T23:59:59Z"
    r = api_client.post(f"/api/v1/user/{user_id}/accesses", json={
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": end_at_1,
        "full_access": True,
    })
    assert r.status_code == 201
    access_id_1 = r.json()["id"]
    user = api_client.get(f"/api/v1/user/{user_id}").json()
    assert user["expire_date"] is not None
    assert user["expire_date"].startswith("2026-06-30")

    # 3. Второй допуск с более поздней датой — expire_date должен вырасти
    end_at_2 = "2026-12-31T23:59:59Z"
    r = api_client.post(f"/api/v1/user/{user_id}/accesses", json={
        "start_at": "2026-01-01T00:00:00Z",
        "end_at": end_at_2,
        "full_access": True,
    })
    assert r.status_code == 201
    access_id_2 = r.json()["id"]
    user = api_client.get(f"/api/v1/user/{user_id}").json()
    assert user["expire_date"].startswith("2026-12-31")

    # 4. Деактивируем второй (более поздний) допуск — expire_date возвращается к первому
    assert api_client.delete(f"/api/v1/access/{access_id_2}").status_code == 204
    user = api_client.get(f"/api/v1/user/{user_id}").json()
    assert user["expire_date"].startswith("2026-06-30")

    # 5. Меняем end_at первого допуска на null — expire_date становится null
    assert api_client.patch(f"/api/v1/access/{access_id_1}", json={"end_at": None}).status_code == 200
    user = api_client.get(f"/api/v1/user/{user_id}").json()
    assert user["expire_date"] is None

    # 6. Реактивируем второй допуск — expire_date возвращается к 2026-12-31
    assert api_client.post(f"/api/v1/access/{access_id_2}/reactivate").status_code == 200
    user = api_client.get(f"/api/v1/user/{user_id}").json()
    assert user["expire_date"].startswith("2026-12-31")

    # Cleanup
    api_client.delete(f"/api/v1/user/{user_id}")


def test_access_granted_by_field(api_client):
    """Проверка правильного заполнения поля granted_by и granted_by_full_name"""
    # 1. Создаем пользователя
    user_payload = {
        "phone": generate_unique_phone(),
        "full_name": "Granted By Test User"
    }
    response = api_client.post("/api/v1/users", json=user_payload)
    assert response.status_code == 201
    user_id = response.json()["id"]

    # 2. Создаем допуск (granted_by должен быть установлен автоматически из X-Remote-User)
    access_payload = {
        "start_at": "2026-03-29T00:00:00Z",
        "full_access": True
    }
    response = api_client.post(f"/api/v1/user/{user_id}/accesses", json=access_payload)
    assert response.status_code == 201
    access = response.json()
    assert access["granted_by"] == int(REMOTE_USER_ID)
    assert "granted_by_full_name" in access
    assert access["granted_by_full_name"] is not None  # Имя системного пользователя

    # 3. Удаляем пользователя
    response = api_client.delete(f"/api/v1/user/{user_id}")
    assert response.status_code == 204
