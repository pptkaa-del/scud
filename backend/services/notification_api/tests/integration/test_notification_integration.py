"""Интеграционные тесты для Notification API"""
import time


def test_persistent_notification_lifecycle(api_client):
    """Тест: жизненный цикл persistent уведомления"""
    # 1. Создаем persistent уведомление с ролями
    payload = {
        "type": "persistent",
        "text": "Шкаф в состоянии alarm",
        "tags": {"locker_id": "550e8400-e29b-41d4-a716-446655440000", "severity": "high"},
        "start_at": "2024-01-15T12:00:00Z",
        "roles": ["operator", "administrator"],
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert "notification" in data
    assert "recipients" in data
    notification_id = data["notification"]["id"]
    assert data["notification"]["type"] == "persistent"
    assert data["notification"]["is_cancelled"] is False
    assert len(data["recipients"]) == 2

    # 2. Получаем по ID
    response = api_client.get(f"/api/v1/notification/{notification_id}")
    assert response.status_code == 200
    detail = response.json()
    assert detail["notification"]["text"] == "Шкаф в состоянии alarm"
    assert len(detail["recipients"]) == 2

    # 3. Видно в списке (paginated envelope)
    response = api_client.get("/api/v1/notifications")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total", "limit", "offset"} <= body.keys()
    ids = [n["id"] for n in body["items"]]
    assert notification_id in ids

    # 4. Отменяем
    response = api_client.delete(f"/api/v1/notification/{notification_id}")
    assert response.status_code == 200
    assert response.json()["is_cancelled"] is True

    # 5. Не видно в active_only=true
    response = api_client.get("/api/v1/notifications?active_only=true")
    assert response.status_code == 200
    active_ids = [n["id"] for n in response.json()["items"]]
    assert notification_id not in active_ids

    # 6. Видно в active_only=false
    response = api_client.get("/api/v1/notifications?active_only=false")
    assert response.status_code == 200
    all_ids = [n["id"] for n in response.json()["items"]]
    assert notification_id in all_ids


def test_notifications_list_pagination_envelope(api_client):
    """Список уведомлений имеет PaginatedResponse-формат."""
    response = api_client.get("/api/v1/notifications?limit=10&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total", "limit", "offset"} <= body.keys()
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert isinstance(body["items"], list)
    assert body["total"] >= len(body["items"])


def test_one_time_sms_type_rejected(api_client):
    """Бывший SMS-тип one_time_sms больше не поддерживается → 400."""
    payload = {
        "type": "one_time_sms",
        "text": "Код открытия: 123456",
        "scheduled_at": "2099-12-31T23:59:59Z",
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 400


def test_periodic_sms_type_rejected(api_client):
    """Бывший SMS-тип periodic_sms больше не поддерживается → 400."""
    payload = {
        "type": "periodic_sms",
        "text": "Периодический отчет",
        "interval_minutes": 60,
        "start_at": "2099-01-15T09:00:00Z",
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 400


def test_persistent_with_user_ids(api_client):
    """Тест: persistent уведомление с user_ids, фильтрация по user_id"""
    payload = {
        "type": "persistent",
        "text": "Уведомление для конкретных пользователей",
        "start_at": "2024-01-15T12:00:00Z",
        "user_ids": [1, 2, 3],
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 201
    notification_id = response.json()["notification"]["id"]

    # Фильтрация по user_id=1 — должно найтись
    response = api_client.get("/api/v1/notifications?user_id=1")
    assert response.status_code == 200
    ids = [n["id"] for n in response.json()["items"]]
    assert notification_id in ids

    # Фильтрация по user_id=999 — не должно найтись
    response = api_client.get("/api/v1/notifications?user_id=999")
    assert response.status_code == 200
    ids = [n["id"] for n in response.json()["items"]]
    assert notification_id not in ids

    # Cleanup
    api_client.delete(f"/api/v1/notification/{notification_id}")


def test_filter_by_role(api_client):
    """Тест: фильтрация persistent по роли"""
    payload = {
        "type": "persistent",
        "text": "Только для операторов",
        "start_at": "2024-01-15T12:00:00Z",
        "roles": ["operator"],
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 201
    notification_id = response.json()["notification"]["id"]

    # Фильтр по роли operator — найдено
    response = api_client.get("/api/v1/notifications?role=operator")
    assert response.status_code == 200
    ids = [n["id"] for n in response.json()["items"]]
    assert notification_id in ids

    # Фильтр по роли worker — не найдено
    response = api_client.get("/api/v1/notifications?role=worker")
    assert response.status_code == 200
    ids = [n["id"] for n in response.json()["items"]]
    assert notification_id not in ids

    # Cleanup
    api_client.delete(f"/api/v1/notification/{notification_id}")


def test_filter_by_tags(api_client):
    """Тест: фильтрация по тегам"""
    unique_tag = f"test-{int(time.time() * 1000)}"
    payload = {
        "type": "persistent",
        "text": "С тегами",
        "tags": {"category": unique_tag, "priority": "high"},
        "start_at": "2024-01-15T12:00:00Z",
        "roles": ["operator"],
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 201
    notification_id = response.json()["notification"]["id"]

    # Фильтр по тегу — найдено
    response = api_client.get(
        f"/api/v1/notifications?tag_key=category&tag_value={unique_tag}")
    assert response.status_code == 200
    ids = [n["id"] for n in response.json()["items"]]
    assert notification_id in ids

    # Фильтр по другому значению — не найдено
    response = api_client.get(
        "/api/v1/notifications?tag_key=category&tag_value=nonexistent")
    assert response.status_code == 200
    ids = [n["id"] for n in response.json()["items"]]
    assert notification_id not in ids

    # Cleanup
    api_client.delete(f"/api/v1/notification/{notification_id}")


def test_filter_by_type(api_client):
    """Тест: фильтрация по типу"""
    response = api_client.get("/api/v1/notifications?type=persistent")
    assert response.status_code == 200
    for n in response.json()["items"]:
        assert n["type"] == "persistent"


def test_get_nonexistent_notification(api_client):
    """Тест: получение несуществующего уведомления"""
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    response = api_client.get(f"/api/v1/notification/{fake_uuid}")
    assert response.status_code == 404


def test_cancel_already_cancelled(api_client):
    """Тест: повторная отмена → 404"""
    payload = {
        "type": "persistent",
        "text": "Для отмены",
        "start_at": "2024-01-15T12:00:00Z",
        "roles": ["operator"],
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 201
    notification_id = response.json()["notification"]["id"]

    # Первая отмена — успех
    response = api_client.delete(f"/api/v1/notification/{notification_id}")
    assert response.status_code == 200

    # Повторная отмена — 404
    response = api_client.delete(f"/api/v1/notification/{notification_id}")
    assert response.status_code == 404


def test_validation_empty_text(api_client):
    """Тест: пустой text → 400"""
    payload = {
        "type": "persistent",
        "text": "",
        "start_at": "2024-01-15T12:00:00Z",
        "roles": ["operator"],
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 400


def test_validation_missing_required_fields(api_client):
    """Тест: отсутствие обязательных полей → 400"""
    # persistent без start_at
    payload = {
        "type": "persistent",
        "text": "Тест",
        "roles": ["operator"],
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 400

    # persistent без user_ids и roles
    payload = {
        "type": "persistent",
        "text": "Тест",
        "start_at": "2024-01-15T12:00:00Z",
    }
    response = api_client.post("/api/v1/notifications", json=payload)
    assert response.status_code == 400


def test_unauthorized_access(wait_for_api):
    """Тест: запрос без X-Remote-User → 401"""
    import requests

    from tests.integration.conftest import BASE_URL

    response = requests.get(f"{BASE_URL}/api/v1/notifications")
    assert response.status_code == 401
