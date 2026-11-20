"""Интеграционные тесты для Security Events API"""
from datetime import datetime, timedelta

from .conftest import generate_unique_locker_id, generate_unique_sensor_id


def test_security_event_lifecycle(api_client):
    """Тест: полный жизненный цикл события безопасности"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # 1. Создаем событие безопасности
    payload = {
        "locker_id": locker_id,
        "sensor_id": sensor_id,
        "event_type": "threshold_violation",
        "severity": "high",
        "violated_thresholds": [
            {
                "threshold_id": 123,
                "sensor_type": "temperature",
                "value": 45.0,
                "max_allowed": 35.0
            }
        ],
        "metadata": {
            "source": "state_monitor"
        }
    }

    response = api_client.post("/api/v1/events/security", json=payload)
    assert response.status_code == 201
    event = response.json()
    assert event["locker_id"] == locker_id
    assert event["event_type"] == "threshold_violation"
    assert event["severity"] == "high"
    assert "id" in event
    assert "occurred_at" in event
    event_id = event["id"]

    # 2. Получаем список событий с фильтром по locker_id
    response = api_client.get(f"/api/v1/events/security?locker_id={locker_id}")
    assert response.status_code == 200
    events = response.json()
    assert "items" in events
    assert events["total"] >= 1

    # 3. Закрываем событие. COR-08: resolved_by берётся из X-Remote-User
    # (системный пользователь 1 в conftest), а не из тела запроса.
    response = api_client.patch(f"/api/v1/events/security/{event_id}", json={})
    assert response.status_code == 200
    resolved_event = response.json()
    assert resolved_event["resolved_by"] == 1
    assert resolved_event["resolved_at"] is not None


def test_security_events_list_filters(api_client):
    """Тест: фильтрация списка событий безопасности"""
    locker_id = generate_unique_locker_id()

    # 1. Создаем несколько событий разных типов и уровней
    events_data = [
        {"event_type": "threshold_violation", "severity": "high"},
        {"event_type": "intrusion", "severity": "critical"},
        {"event_type": "offline", "severity": "medium"},
    ]

    for event_data in events_data:
        payload = {
            "locker_id": locker_id,
            "event_type": event_data["event_type"],
            "severity": event_data["severity"]
        }
        response = api_client.post("/api/v1/events/security", json=payload)
        assert response.status_code == 201

    # 2. Фильтр по event_type
    response = api_client.get("/api/v1/events/security?event_type=intrusion")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)

    # 3. Фильтр по severity
    response = api_client.get("/api/v1/events/security?severity=critical")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)

    # 4. Фильтр по locker_id
    response = api_client.get(f"/api/v1/events/security?locker_id={locker_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3

    # 5. Фильтр по resolved (неразрешенные события)
    response = api_client.get(f"/api/v1/events/security?locker_id={locker_id}&resolved=false")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3
    # Проверяем, что все события неразрешенные
    for item in data["items"]:
        if item["locker_id"] == locker_id:
            assert item["resolved_at"] is None

    # 6. Фильтр по датам
    now = datetime.utcnow()
    from_date = (now - timedelta(hours=1)).isoformat() + "Z"
    to_date = (now + timedelta(hours=1)).isoformat() + "Z"
    response = api_client.get(
        f"/api/v1/events/security?locker_id={locker_id}&from={from_date}&to={to_date}"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 3


def test_resolve_event(api_client):
    """Тест: закрытие события безопасности"""
    locker_id = generate_unique_locker_id()

    # 1. Создаем событие
    payload = {
        "locker_id": locker_id,
        "event_type": "door_tamper",
        "severity": "critical"
    }
    response = api_client.post("/api/v1/events/security", json=payload)
    assert response.status_code == 201
    event_id = response.json()["id"]

    # 2. Закрываем событие. COR-08: resolved_by = аутентифицированный
    # пользователь (X-Remote-User=1 из conftest), тело resolved_by игнорируется.
    response = api_client.patch(f"/api/v1/events/security/{event_id}", json={})
    assert response.status_code == 200
    resolved_event = response.json()
    assert resolved_event["id"] == event_id
    assert resolved_event["resolved_by"] == 1
    assert resolved_event["resolved_at"] is not None

    # 2a. COR-08: повторное закрытие уже закрытого события → 409 (идемпотентность).
    response = api_client.patch(f"/api/v1/events/security/{event_id}", json={})
    assert response.status_code == 409

    # 3. Проверяем, что событие теперь в списке разрешенных
    response = api_client.get(f"/api/v1/events/security?locker_id={locker_id}&resolved=true")
    assert response.status_code == 200
    data = response.json()
    found = False
    for item in data["items"]:
        if item["id"] == event_id:
            found = True
            assert item["resolved_at"] is not None
            break
    assert found


def test_resolve_nonexistent_event(api_client):
    """Тест: попытка закрыть несуществующее событие"""
    nonexistent_id = 999999999

    response = api_client.patch(f"/api/v1/events/security/{nonexistent_id}", json={})
    assert response.status_code == 404
    error = response.json()
    assert "detail" in error


def test_security_event_validation(api_client):
    """Тест: валидация событий безопасности.

    `locker_id` ОПЦИОНАЛЕН (для audit-событий вне домена шкафа, например
    ``command_2fa_failed`` при ненайденном challenge_id), поэтому пустой
    payload без locker_id с валидным event_type/severity — 201. Остаются
    проверки на невалидный event_type и severity.
    """
    # 1. Недопустимое значение event_type
    payload = {
        "locker_id": generate_unique_locker_id(),
        "event_type": "invalid_type",
        "severity": "high"
    }
    response = api_client.post("/api/v1/events/security", json=payload)
    assert response.status_code == 400

    # 2. Недопустимое значение severity
    payload = {
        "locker_id": generate_unique_locker_id(),
        "event_type": "intrusion",
        "severity": "super_critical"  # Неверное значение
    }
    response = api_client.post("/api/v1/events/security", json=payload)
    assert response.status_code == 400

    # 3. Отсутствие обязательных полей event_type/severity → 400.
    payload = {"locker_id": generate_unique_locker_id()}
    response = api_client.post("/api/v1/events/security", json=payload)
    assert response.status_code == 400


def test_security_event_pagination(api_client):
    """Тест: пагинация списка событий"""
    locker_id = generate_unique_locker_id()

    # 1. Создаем несколько событий
    for _i in range(5):
        payload = {
            "locker_id": locker_id,
            "event_type": "offline",
            "severity": "low"
        }
        response = api_client.post("/api/v1/events/security", json=payload)
        assert response.status_code == 201

    # 2. Получаем с лимитом
    response = api_client.get(f"/api/v1/events/security?locker_id={locker_id}&limit=3")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) <= 3
    assert data["limit"] == 3

    # 3. Получаем со смещением
    response = api_client.get(f"/api/v1/events/security?locker_id={locker_id}&limit=2&offset=2")
    assert response.status_code == 200
    data = response.json()
    assert data["offset"] == 2


def test_security_event_with_violated_thresholds(api_client):
    """Тест: событие с нарушенными пороговыми значениями"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    payload = {
        "locker_id": locker_id,
        "sensor_id": sensor_id,
        "event_type": "threshold_violation",
        "severity": "high",
        "violated_thresholds": [
            {
                "threshold_id": 456,
                "sensor_type": "humidity",
                "value": 85.0,
                "max_allowed": 80.0
            },
            {
                "threshold_id": 789,
                "sensor_type": "temperature",
                "value": -5.0,
                "min_allowed": 0.0
            }
        ]
    }

    response = api_client.post("/api/v1/events/security", json=payload)
    assert response.status_code == 201
    event = response.json()
    assert len(event["violated_thresholds"]) == 2
    assert event["sensor_id"] == sensor_id
