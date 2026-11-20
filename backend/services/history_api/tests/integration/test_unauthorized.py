"""Интеграционные тесты для проверки авторизации"""
import os

import requests

# Конфигурация из переменных окружения или дефолты
BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8002")


def test_telemetry_endpoints_without_auth():
    """Тест: эндпоинты телеметрии без заголовка X-Remote-User"""
    # POST /api/v1/telemetry
    payload = {
        "readings": [
            {
                "sensor_id": "660e8400-e29b-41d4-a716-446655440111",
                "sensor_type_code": "temperature",
                "value": 25.0
            }
        ]
    }
    response = requests.post(f"{BASE_URL}/api/v1/telemetry", json=payload)
    assert response.status_code == 401
    assert "detail" in response.json()

    # GET /api/v1/telemetry/list
    response = requests.get(f"{BASE_URL}/api/v1/telemetry/list")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_security_events_endpoints_without_auth():
    """Тест: эндпоинты событий безопасности без авторизации"""
    # GET /api/v1/events/security
    response = requests.get(f"{BASE_URL}/api/v1/events/security")
    assert response.status_code == 401
    assert "detail" in response.json()

    # POST /api/v1/events/security
    payload = {
        "locker_id": "550e8400-e29b-41d4-a716-446655440000",
        "event_type": "intrusion",
        "severity": "critical"
    }
    response = requests.post(f"{BASE_URL}/api/v1/events/security", json=payload)
    assert response.status_code == 401
    assert "detail" in response.json()

    # PATCH /api/v1/events/security/{eventId}
    response = requests.patch(
        f"{BASE_URL}/api/v1/events/security/123",
        json={"resolved_by": 1}
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_command_history_endpoints_without_auth():
    """Тест: эндпоинты истории команд без авторизации"""
    # GET /api/v1/commands/history
    response = requests.get(f"{BASE_URL}/api/v1/commands/history")
    assert response.status_code == 401
    assert "detail" in response.json()

    # POST /api/v1/commands/history
    payload = {
        "command_id": "880e8400-e29b-41d4-a716-446655440333",
        "locker_id": "550e8400-e29b-41d4-a716-446655440000",
        "command_type": "open",
        "status": "pending"
    }
    response = requests.post(f"{BASE_URL}/api/v1/commands/history", json=payload)
    assert response.status_code == 401
    assert "detail" in response.json()


def test_media_endpoints_without_auth():
    """Тест: эндпоинты медиа без авторизации"""
    # GET /api/v1/media
    response = requests.get(f"{BASE_URL}/api/v1/media")
    assert response.status_code == 401
    assert "detail" in response.json()

    # GET /api/v1/media/{mediaId}
    response = requests.get(f"{BASE_URL}/api/v1/media/123")
    assert response.status_code == 401
    assert "detail" in response.json()

    # GET /api/v1/events/security/{id}/media
    response = requests.get(f"{BASE_URL}/api/v1/events/security/1/media")
    assert response.status_code == 401
    assert "detail" in response.json()


def test_analytics_endpoints_without_auth():
    """Тест: эндпоинты аналитики без авторизации"""
    # GET /api/v1/analytics/sensors
    response = requests.get(
        f"{BASE_URL}/api/v1/analytics/sensors?sensor_id=660e8400-e29b-41d4-a716-446655440111"
    )
    assert response.status_code == 401
    assert "detail" in response.json()

    # GET /api/v1/analytics/lockers/{lockerId}/summary
    response = requests.get(
        f"{BASE_URL}/api/v1/analytics/lockers/550e8400-e29b-41d4-a716-446655440000/summary"
    )
    assert response.status_code == 401
    assert "detail" in response.json()


def test_invalid_auth_header():
    """Тест: запросы с некорректным значением X-Remote-User"""
    headers = {"X-Remote-User": "invalid_value"}  # Должен быть числовой ID

    # Проверяем на примере одного эндпоинта
    response = requests.get(
        f"{BASE_URL}/api/v1/telemetry/list",
        headers=headers
    )
    # Может вернуть 400 (Bad Request) или 401 в зависимости от валидации
    assert response.status_code in [400, 401]


def test_empty_auth_header():
    """Тест: запросы с пустым X-Remote-User"""
    headers = {"X-Remote-User": ""}

    response = requests.get(
        f"{BASE_URL}/api/v1/telemetry/list",
        headers=headers
    )
    # Пустое значение должно рассматриваться как отсутствие авторизации
    assert response.status_code in [400, 401]


def test_all_endpoints_require_auth():
    """Тест: все эндпоинты API требуют авторизации"""
    # Список всех эндпоинтов для проверки
    endpoints_to_test = [
        ("POST", "/api/v1/telemetry", {"readings": []}),
        ("GET", "/api/v1/telemetry/list", None),
        ("GET", "/api/v1/events/security", None),
        ("POST", "/api/v1/events/security", {"locker_id": "test", "event_type": "intrusion", "severity": "high"}),
        ("GET", "/api/v1/commands/history", None),
        ("POST", "/api/v1/commands/history", {"command_id": "test", "locker_id": "test", "command_type": "open", "status": "pending"}),
        ("GET", "/api/v1/media", None),
        ("GET", "/api/v1/media/1", None),
        ("GET", "/api/v1/events/security/1/media", None),
    ]

    for method, endpoint, payload in endpoints_to_test:
        url = f"{BASE_URL}{endpoint}"

        if method == "GET":
            response = requests.get(url)
        elif method == "POST":
            response = requests.post(url, json=payload)
        elif method == "DELETE":
            response = requests.delete(url)
        elif method == "PATCH":
            response = requests.patch(url, json=payload)

        # Все эндпоинты должны возвращать 401 без авторизации
        assert response.status_code in [401, 400], f"Endpoint {method} {endpoint} returned {response.status_code}"


def test_valid_auth_header():
    """Тест: запросы с корректным X-Remote-User проходят авторизацию"""
    headers = {"X-Remote-User": "1"}

    # Проверяем, что с валидным заголовком запрос не возвращает 401
    response = requests.get(
        f"{BASE_URL}/api/v1/telemetry/list",
        headers=headers
    )
    # Должен вернуть 200 или другой код, но НЕ 401
    assert response.status_code != 401


def test_numeric_user_id_formats():
    """Тест: различные форматы числового user_id"""
    # Проверяем разные валидные форматы
    valid_user_ids = ["1", "123", "999999"]

    for user_id in valid_user_ids:
        headers = {"X-Remote-User": user_id}
        response = requests.get(
            f"{BASE_URL}/api/v1/telemetry/list",
            headers=headers
        )
        # Не должен возвращать 401 (авторизация успешна)
        assert response.status_code != 401, f"User ID {user_id} failed authorization"
