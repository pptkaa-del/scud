"""Интеграционные тесты для Telemetry API"""
from datetime import datetime, timedelta

import psycopg2

from .conftest import DB_CONFIG, generate_unique_locker_id, generate_unique_sensor_id


def test_telemetry_batch_create(api_client):
    """Тест: batch-запись телеметрии"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # 1. Отправляем batch телеметрии
    payload = {
        "locker_id": locker_id,
        "readings": [
            {
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "value": 23.7,
                "recorded_at": datetime.utcnow().isoformat() + "Z"
            },
            {
                "sensor_id": sensor_id,
                "sensor_type_code": "humidity",
                "value": 65.0,
                "recorded_at": datetime.utcnow().isoformat() + "Z"
            }
        ]
    }

    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 201
    result = response.json()
    assert "inserted" in result
    assert result["inserted"] == 2


def test_telemetry_list_with_filters(api_client):
    """Тест: получение телеметрии с фильтрами"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # 1. Создаем тестовые данные
    now = datetime.utcnow()
    payload = {
        "locker_id": locker_id,
        "readings": [
            {
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "value": 25.5,
                "recorded_at": now.isoformat() + "Z"
            }
        ]
    }
    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 201

    # 2. Получаем данные с фильтром по locker_id
    response = api_client.get(f"/api/v1/telemetry/list?locker_id={locker_id}")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert data["total"] >= 1

    # 3. Фильтр по sensor_id
    response = api_client.get(f"/api/v1/telemetry/list?sensor_id={sensor_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1

    # 4. Фильтр по sensor_type_code
    response = api_client.get("/api/v1/telemetry/list?sensor_type_code=temperature")
    assert response.status_code == 200
    data = response.json()
    assert isinstance(data["items"], list)

    # 5. Фильтр по датам
    from_date = (now - timedelta(hours=1)).isoformat() + "Z"
    to_date = (now + timedelta(hours=1)).isoformat() + "Z"
    response = api_client.get(
        f"/api/v1/telemetry/list?from={from_date}&to={to_date}&locker_id={locker_id}"
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1


def test_telemetry_pagination(api_client):
    """Тест: пагинация телеметрии"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # 1. Создаем несколько записей
    readings = []
    for i in range(5):
        readings.append({
            "sensor_id": sensor_id,
            "sensor_type_code": "temperature",
            "value": 20.0 + i,
            "recorded_at": datetime.utcnow().isoformat() + "Z"
        })

    payload = {"locker_id": locker_id, "readings": readings}
    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 201
    assert response.json()["inserted"] == 5

    # 2. Получаем с лимитом
    response = api_client.get(f"/api/v1/telemetry/list?locker_id={locker_id}&limit=3")
    assert response.status_code == 200
    data = response.json()
    assert len(data["items"]) <= 3
    assert data["limit"] == 3
    assert data["offset"] == 0

    # 3. Получаем со смещением
    response = api_client.get(f"/api/v1/telemetry/list?locker_id={locker_id}&limit=2&offset=2")
    assert response.status_code == 200
    data = response.json()
    assert data["offset"] == 2


def test_telemetry_batch_validation(api_client):
    """Тест: валидация batch запросов"""
    # 1. Пустой массив readings
    payload = {"readings": []}
    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 400

    # 2. Отсутствует обязательное поле
    payload = {
        "readings": [
            {"sensor_id": generate_unique_sensor_id(), "value": 25.0}
            # Отсутствует sensor_type_code
        ]
    }
    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 400

    # 3. Слишком много записей (>1000)
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()
    readings = []
    for _i in range(1001):
        readings.append({
            "sensor_id": sensor_id,
            "sensor_type_code": "temperature",
            "value": 25.0
        })
    payload = {"locker_id": locker_id, "readings": readings}
    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 400


def test_telemetry_text_value(api_client):
    """Тест: телеметрия с текстовым значением (для булевых датчиков)"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    payload = {
        "locker_id": locker_id,
        "readings": [
            {
                "sensor_id": sensor_id,
                "sensor_type_code": "door",
                "value_text": "open",
                "recorded_at": datetime.utcnow().isoformat() + "Z"
            }
        ]
    }

    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 201
    assert response.json()["inserted"] == 1


def test_telemetry_with_metadata(api_client):
    """Тест: телеметрия с метаданными"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    payload = {
        "locker_id": locker_id,
        "readings": [
            {
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "value": 24.5,
                "metadata": {
                    "source": "device",
                    "battery": "90%"
                }
            }
        ]
    }

    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 201
    assert response.json()["inserted"] == 1


def test_telemetry_out_of_range_lands_in_default_partition(api_client):
    """COR-06 real-DB: запись с recorded_at вне диапазона месячных партиций
    вставляется без ошибки и попадает в DEFAULT-партицию (safety net).

    Месячные партиции создаются retention-сервисом только для окна
    [текущий месяц .. +LOOKAHEAD]. Дата из далёкого прошлого (2000 г.)
    гарантированно не покрыта ни одной YYYY_MM-партицией, поэтому Postgres
    маршрутизирует строку в sensor_telemetry_default. Без DEFAULT-партиции /
    без per-month try-изоляции такая вставка падала бы.
    """
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    payload = {
        "locker_id": locker_id,
        "readings": [
            {
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "value": 1.0,
                "recorded_at": "2000-01-15T10:00:00Z",
            }
        ],
    }
    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 201
    assert response.json()["inserted"] == 1

    # Подтверждаем по БД, что строка физически легла именно в DEFAULT-партицию
    # (tableoid::regclass даёт имя конкретной партиции, где лежит строка).
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tableoid::regclass::text
                FROM sensor_telemetry
                WHERE sensor_id = %s
                """,
                (sensor_id,),
            )
            row = cur.fetchone()
            assert row is not None, "row was not inserted"
            assert row[0] == "sensor_telemetry_default", (
                f"expected default partition, got {row[0]}"
            )
    finally:
        conn.close()
