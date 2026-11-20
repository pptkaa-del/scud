"""Интеграционные тесты для Analytics API"""
from datetime import datetime, timedelta

from .conftest import generate_unique_locker_id, generate_unique_sensor_id


def create_telemetry_data(api_client, locker_id, sensor_id, count=10):
    """Вспомогательная функция для создания тестовых данных телеметрии"""
    readings = []
    base_time = datetime.utcnow()

    for i in range(count):
        readings.append({
            "sensor_id": sensor_id,
            "sensor_type_code": "temperature",
            "value": 20.0 + i * 0.5,
            "recorded_at": (base_time - timedelta(hours=i)).isoformat() + "Z"
        })

    payload = {"locker_id": locker_id, "readings": readings}
    response = api_client.post("/api/v1/telemetry", json=payload)
    assert response.status_code == 201
    assert response.json()["inserted"] == count


def test_sensor_analytics(api_client):
    """Тест: получение агрегированных данных датчика"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # 1. Создаем тестовые данные телеметрии
    create_telemetry_data(api_client, locker_id, sensor_id, count=24)

    # Даем время на агрегацию (в реальности это фоновый процесс)
    # В тестах может потребоваться подождать или использовать моки

    # 2. Запрашиваем агрегированные данные
    response = api_client.get(f"/api/v1/analytics/sensors?sensor_id={sensor_id}")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert isinstance(data["items"], list)


def test_sensor_analytics_with_aggregation_type(api_client):
    """Тест: аналитика с разными типами агрегации"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # Создаем данные
    create_telemetry_data(api_client, locker_id, sensor_id, count=48)

    # Проверяем разные типы агрегации
    aggregation_types = ["hourly", "daily", "monthly"]

    for agg_type in aggregation_types:
        response = api_client.get(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}&aggregation_type={agg_type}"
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data


def test_sensor_analytics_with_metrics(api_client):
    """Тест: аналитика с разными метриками"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # Создаем данные
    create_telemetry_data(api_client, locker_id, sensor_id, count=20)

    # Проверяем разные метрики
    metrics = ["avg", "min", "max", "sum", "stddev"]

    for metric in metrics:
        response = api_client.get(
            f"/api/v1/analytics/sensors?sensor_id={sensor_id}&metric={metric}"
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data


def test_sensor_analytics_date_range(api_client):
    """Тест: аналитика с фильтрацией по датам"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # Создаем данные
    create_telemetry_data(api_client, locker_id, sensor_id, count=30)

    # Запрашиваем данные за определенный период
    now = datetime.utcnow()
    from_date = (now - timedelta(days=7)).isoformat() + "Z"
    to_date = now.isoformat() + "Z"

    response = api_client.get(
        f"/api/v1/analytics/sensors?sensor_id={sensor_id}&from={from_date}&to={to_date}"
    )
    assert response.status_code == 200
    data = response.json()
    assert "items" in data


def test_sensor_analytics_validation(api_client):
    """Тест: валидация запросов аналитики"""
    # 1. Отсутствует обязательный параметр sensor_id
    response = api_client.get("/api/v1/analytics/sensors")
    assert response.status_code == 400
    error = response.json()
    assert "detail" in error

    # 2. Недопустимое значение aggregation_type
    sensor_id = generate_unique_sensor_id()
    response = api_client.get(
        f"/api/v1/analytics/sensors?sensor_id={sensor_id}&aggregation_type=invalid"
    )
    assert response.status_code == 400

    # 3. Недопустимое значение metric
    response = api_client.get(
        f"/api/v1/analytics/sensors?sensor_id={sensor_id}&metric=invalid_metric"
    )
    assert response.status_code == 400


def test_locker_summary(api_client):
    """Тест: получение сводки по шкафу"""
    locker_id = generate_unique_locker_id()

    # Создаем датчики разных типов
    sensor_ids = {
        "temperature": generate_unique_sensor_id(),
        "humidity": generate_unique_sensor_id(),
        "door": generate_unique_sensor_id()
    }

    # Создаем данные для каждого датчика
    for sensor_type, sensor_id in sensor_ids.items():
        readings = []
        for i in range(10):
            reading = {
                "sensor_id": sensor_id,
                "sensor_type_code": sensor_type,
                "recorded_at": (datetime.utcnow() - timedelta(hours=i)).isoformat() + "Z"
            }
            if sensor_type == "door":
                reading["value_text"] = "closed" if i % 2 == 0 else "open"
            else:
                reading["value"] = 20.0 + i
            readings.append(reading)

        payload = {"locker_id": locker_id, "readings": readings}
        response = api_client.post("/api/v1/telemetry", json=payload)
        assert response.status_code == 201

    # Запрашиваем сводку по шкафу
    response = api_client.get(f"/api/v1/analytics/lockers/{locker_id}/summary")
    assert response.status_code == 200
    summary = response.json()
    assert "locker_id" in summary
    assert summary["locker_id"] == locker_id
    assert "sensors" in summary
    assert isinstance(summary["sensors"], list)
    assert "period_start" in summary
    assert "period_end" in summary


def test_locker_summary_with_aggregation_type(api_client):
    """Тест: сводка по шкафу с разными типами агрегации"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # Создаем данные
    create_telemetry_data(api_client, locker_id, sensor_id, count=30)

    # Проверяем разные типы агрегации
    aggregation_types = ["hourly", "daily", "monthly"]

    for agg_type in aggregation_types:
        response = api_client.get(
            f"/api/v1/analytics/lockers/{locker_id}/summary?aggregation_type={agg_type}"
        )
        assert response.status_code == 200
        summary = response.json()
        assert "locker_id" in summary
        assert summary["locker_id"] == locker_id


def test_locker_summary_date_range(api_client):
    """Тест: сводка по шкафу за определенный период"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # Создаем данные
    create_telemetry_data(api_client, locker_id, sensor_id, count=50)

    # Запрашиваем сводку за период
    now = datetime.utcnow()
    from_date = (now - timedelta(days=30)).isoformat() + "Z"
    to_date = now.isoformat() + "Z"

    response = api_client.get(
        f"/api/v1/analytics/lockers/{locker_id}/summary?from={from_date}&to={to_date}"
    )
    assert response.status_code == 200
    summary = response.json()
    assert "locker_id" in summary
    assert "period_start" in summary
    assert "period_end" in summary


def test_locker_summary_nonexistent_locker(api_client):
    """Тест: запрос сводки для несуществующего шкафа"""
    nonexistent_locker_id = generate_unique_locker_id()

    response = api_client.get(f"/api/v1/analytics/lockers/{nonexistent_locker_id}/summary")
    assert response.status_code == 404
    error = response.json()
    assert "detail" in error


def test_locker_summary_multiple_sensors(api_client):
    """Тест: сводка по шкафу с несколькими датчиками"""
    locker_id = generate_unique_locker_id()

    # Создаем 3 датчика температуры с разными значениями
    for sensor_num in range(3):
        sensor_id = generate_unique_sensor_id()
        readings = []
        for i in range(5):
            readings.append({
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "value": 15.0 + sensor_num * 5 + i,
                "recorded_at": (datetime.utcnow() - timedelta(hours=i)).isoformat() + "Z"
            })
        payload = {"locker_id": locker_id, "readings": readings}
        response = api_client.post("/api/v1/telemetry", json=payload)
        assert response.status_code == 201

    # Запрашиваем сводку
    response = api_client.get(f"/api/v1/analytics/lockers/{locker_id}/summary")
    assert response.status_code == 200
    summary = response.json()
    assert "sensors" in summary
    # Ожидаем данные по всем 3 датчикам (или агрегированные данные)


def test_analytics_empty_results(api_client):
    """Тест: аналитика для датчика без данных"""
    # Используем sensor_id для которого нет данных
    empty_sensor_id = generate_unique_sensor_id()

    response = api_client.get(f"/api/v1/analytics/sensors?sensor_id={empty_sensor_id}")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert data["total"] == 0
    assert len(data["items"]) == 0


def test_analytics_sensor_aggregation_fields(api_client):
    """Тест: проверка полей в агрегированных данных"""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    # Создаем данные
    create_telemetry_data(api_client, locker_id, sensor_id, count=15)

    # Запрашиваем агрегаты
    response = api_client.get(f"/api/v1/analytics/sensors?sensor_id={sensor_id}")
    assert response.status_code == 200
    data = response.json()

    if data["total"] > 0:
        # Проверяем структуру первого элемента
        item = data["items"][0]
        assert "id" in item
        assert "locker_id" in item
        assert "sensor_id" in item
        assert "sensor_type_code" in item
        assert "aggregation_type" in item
        assert "period_start" in item
        assert "period_end" in item
        assert "sample_count" in item
        # Метрики могут быть null для некоторых типов датчиков (DTO: float | None)
        for field in ("value_avg", "value_min", "value_max"):
            assert field in item, f"{field} missing from item"
            val = item[field]
            assert val is None or isinstance(val, int | float), (
                f"{field} must be null or number, got {type(val).__name__}"
            )
