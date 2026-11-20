"""Интеграционные тесты для Locker API"""
import os
import time
import uuid

import psycopg2


def _db_connect():
    """Прямое подключение к scud_lockers — нужно для проверок инвариантов
    БД, которые нельзя выставить через публичный API (например, door=open
    управляется только из device_handler по WS)."""
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def generate_unique_inventory_number():
    """Генерирует уникальный инвентарный номер на основе UUID4.

    UUID-источник избавляет от глобального состояния — несколько тестов могут
    выполняться параллельно без коллизий по счётчику.
    """
    return f"СО №{uuid.uuid4().hex[:10]}"


def _unique_relay_id() -> int:
    return 500_000_000 + uuid.uuid4().int % 400_000_000


def test_inventory_number_freed_after_soft_delete(api_client):
    """После soft-delete шкафа его inventory_number должен освободиться.

    Регрессионный тест: partial unique index `ux_lockers_inventory_active`
    обеспечивает уникальность только среди активных. Архивные записи
    не должны блокировать создание новых шкафов с тем же номером.
    """
    inventory_number = generate_unique_inventory_number()

    # Создаём первый шкаф
    response = api_client.post("/api/v1/lockers", json={
        "inventory_number": inventory_number,
        "location_address": "ул. Регрессионная, 1",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert response.status_code == 201, response.text
    first_id = response.json()["id"]

    # Архивируем
    response = api_client.delete(f"/api/v1/locker/{first_id}")
    assert response.status_code == 204

    # Создаём новый с тем же inventory_number — должно сработать
    response = api_client.post("/api/v1/lockers", json={
        "inventory_number": inventory_number,
        "location_address": "ул. Регрессионная, 2",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert response.status_code == 201, response.text
    second_id = response.json()["id"]
    assert second_id != first_id


def test_sensor_serial_freed_after_soft_delete(api_client):
    """После soft-delete датчика его (locker_id, serial_number) должны освободиться.

    Партишн-индекс `ux_sensors_serial_locker_active` исключает архивные датчики
    из проверки уникальности.
    """
    locker_response = api_client.post("/api/v1/lockers", json={
        "inventory_number": generate_unique_inventory_number(),
        "location_address": "ул. Сенсорная-регресс, 1",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert locker_response.status_code == 201
    locker_id = locker_response.json()["id"]

    serial = f"TEMP-{uuid.uuid4().hex[:8]}"
    sensor_payload = {"serial_number": serial, "sensor_type_id": 1, "current_value": 25.0}

    # Создаём датчик
    response = api_client.post(f"/api/v1/locker/{locker_id}/sensors", json=sensor_payload)
    assert response.status_code == 201, response.text
    first_sensor_id = response.json()["id"]

    # Soft-delete датчика
    response = api_client.delete(f"/api/v1/locker/{locker_id}/sensors/{first_sensor_id}")
    assert response.status_code == 204, response.text

    # Создаём новый датчик с тем же серийником — должно сработать
    response = api_client.post(f"/api/v1/locker/{locker_id}/sensors", json=sensor_payload)
    assert response.status_code == 201, response.text
    assert response.json()["id"] != first_sensor_id


def test_locker_lifecycle(api_client):
    """Тест: полный жизненный цикл шкафа"""
    inventory_number = generate_unique_inventory_number()

    # 1. Создаем шкаф
    payload = {
        "inventory_number": inventory_number,
        "location_address": "ул. Интеграционная, 1",
        "location_lat": 55.751244,
        "location_lon": 37.618423,
        "description": "Интеграционный тест",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }

    response = api_client.post("/api/v1/lockers", json=payload)
    assert response.status_code == 201
    locker = response.json()
    assert locker["inventory_number"] == inventory_number
    assert "auth_key" not in locker
    locker_id = locker["id"]

    # 2. Получаем шкаф по ID
    response = api_client.get(f"/api/v1/locker/{locker_id}")
    assert response.status_code == 200
    fetched = response.json()
    assert fetched["inventory_number"] == inventory_number
    # Денормализованный кэш времени последнего открытия — null до первой open-команды
    assert "last_opened_at" in fetched
    assert fetched["last_opened_at"] is None

    # 3. Обновляем данные шкафа
    update_payload = {
        "location_address": "ул. Обновленная, 2",
        "description": "Обновленное описание"
    }
    response = api_client.put(f"/api/v1/locker/{locker_id}", json=update_payload)
    assert response.status_code == 200
    updated_locker = response.json()
    assert updated_locker["location_address"] == "ул. Обновленная, 2"

    # 4. Получаем состояние шкафа
    response = api_client.get(f"/api/v1/locker/{locker_id}/state")
    assert response.status_code == 200
    state_data = response.json()
    assert state_data["locker_id"] == locker_id
    assert "connectivity" in state_data
    assert "door" in state_data
    assert "operational_status" in state_data
    assert "last_seen_at" in state_data

    # 5. Обновляем состояние
    response = api_client.put(f"/api/v1/locker/{locker_id}/state",
                               json={"operational_status": "maintenance"})
    assert response.status_code == 200
    assert response.json()["operational_status"] == "maintenance"

    # 6. Удаляем шкаф
    response = api_client.delete(f"/api/v1/locker/{locker_id}")
    assert response.status_code == 204

    # 7. Проверяем, что шкаф удален
    response = api_client.get(f"/api/v1/locker/{locker_id}")
    assert response.status_code == 404


def test_get_lockers_list(api_client):
    """Тест: получение списка шкафов с пагинацией"""
    response = api_client.get("/api/v1/lockers")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data
    assert isinstance(data["items"], list)
    assert data["total"] >= 0


def test_filter_lockers_by_connectivity(api_client):
    """Тест: фильтрация шкафов по connectivity"""
    inv = generate_unique_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    # Новый шкаф — connectivity=offline по умолчанию
    response = api_client.get("/api/v1/lockers?connectivity=offline")
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id in ids

    # Фильтр по online — шкаф не должен быть в списке (он offline)
    response = api_client.get("/api/v1/lockers?connectivity=online")
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id not in ids


def test_filter_lockers_by_operational_status(api_client):
    """Тест: фильтрация шкафов по operational_status"""
    inv = generate_unique_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    # Переводим в maintenance
    resp = api_client.put(f"/api/v1/locker/{locker_id}/state",
                          json={"operational_status": "maintenance"})
    assert resp.status_code == 200

    # Фильтр по maintenance — шкаф должен быть в списке
    response = api_client.get("/api/v1/lockers?operational_status=maintenance")
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id in ids

    # Фильтр по normal — шкаф не должен быть (он в maintenance)
    response = api_client.get("/api/v1/lockers?operational_status=normal")
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id not in ids


def test_filter_lockers_by_door(api_client):
    """Тест: фильтрация шкафов по door"""
    inv = generate_unique_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    # Новый шкаф — door=closed по умолчанию
    response = api_client.get("/api/v1/lockers?door=closed")
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id in ids

    response = api_client.get("/api/v1/lockers?door=open")
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id not in ids


def test_filter_lockers_combined(api_client):
    """Тест: комбинированная фильтрация по нескольким полям"""
    inv = generate_unique_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    # connectivity=offline AND door=closed AND operational_status=normal — подходит
    response = api_client.get(
        "/api/v1/lockers?connectivity=offline&door=closed&operational_status=normal"
    )
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id in ids

    # connectivity=online AND door=closed — не подходит (шкаф offline)
    response = api_client.get("/api/v1/lockers?connectivity=online&door=closed")
    assert response.status_code == 200
    ids = [itm["id"] for itm in response.json()["items"]]
    assert locker_id not in ids


def test_create_locker_duplicate_inventory_number(api_client):
    """Тест: попытка создания шкафа с дублирующимся инвентарным номером"""
    inventory_number = generate_unique_inventory_number()

    payload = {
        "inventory_number": inventory_number,
        "location_address": "ул. Тестовая, 1",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }

    # Создаем первый шкаф
    response = api_client.post("/api/v1/lockers", json=payload)
    assert response.status_code == 201

    # Пытаемся создать второй с тем же номером
    response = api_client.post("/api/v1/lockers", json=payload)
    assert response.status_code == 409


def test_sensor_and_threshold_management(api_client):
    """Тест: управление датчиками и пороговыми значениями"""
    # 1. Создаем шкаф
    locker_payload = {
        "inventory_number": generate_unique_inventory_number(),
        "location_address": "ул. Сенсорная, 1",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }
    response = api_client.post("/api/v1/lockers", json=locker_payload)
    assert response.status_code == 201
    locker_id = response.json()["id"]

    # 2. Создаем датчик температуры
    sensor_payload = {
        "serial_number": f"TEMP-{int(time.time())}",
        "sensor_type_id": 1,  # ID типа "temperature" из sensor_types таблицы
        "current_value": 25.0
    }
    response = api_client.post(f"/api/v1/locker/{locker_id}/sensors",
                                json=sensor_payload)
    assert response.status_code == 201
    sensor = response.json()
    sensor_id = sensor["id"]
    assert sensor["sensor_type_id"] == 1

    # 3. Получаем список датчиков (paginated envelope)
    response = api_client.get(f"/api/v1/locker/{locker_id}/sensors")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total", "limit", "offset"} <= body.keys()
    assert len(body["items"]) >= 1
    assert body["total"] >= 1

    # 4. Создаем пороговое значение для датчика
    threshold_payload = {
        "min_value": -10.0,
        "max_value": 50.0,
        "description": "Допустимая температура"
    }
    response = api_client.post(
        f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
        json=threshold_payload
    )
    assert response.status_code == 201
    threshold = response.json()
    threshold_id = threshold["id"]
    assert threshold["min_value"] == -10.0

    # 5. Получаем список пороговых значений (paginated envelope)
    response = api_client.get(
        f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds"
    )
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total", "limit", "offset"} <= body.keys()
    assert len(body["items"]) >= 1
    assert body["total"] >= 1

    # 6. Обновляем пороговое значение
    update_threshold = {
        "max_value": 45.0,
        "description": "Обновленный порог"
    }
    response = api_client.put(
        f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds/{threshold_id}",
        json=update_threshold
    )
    assert response.status_code == 200

    # 7. Удаляем датчик
    response = api_client.delete(
        f"/api/v1/locker/{locker_id}/sensors/{sensor_id}"
    )
    assert response.status_code == 204

    # 8. Удаляем шкаф
    response = api_client.delete(f"/api/v1/locker/{locker_id}")
    assert response.status_code == 204


def test_command_management(api_client):
    """Тест: управление командами"""
    # 1. Создаем шкаф
    locker_payload = {
        "inventory_number": generate_unique_inventory_number(),
        "location_address": "ул. Командная, 1",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }
    response = api_client.post("/api/v1/lockers", json=locker_payload)
    assert response.status_code == 201
    locker_id = response.json()["id"]

    # 2. Создаем команду открытия
    command_payload = {
        "type": "open",
        "parameters": {}
    }
    response = api_client.post(f"/api/v1/locker/{locker_id}/commands",
                                json=command_payload)
    assert response.status_code == 201
    command = response.json()
    command_id = command["id"]
    assert command["type"] == "open"
    assert command["status"] == "pending"

    # 3. Получаем историю команд (paginated envelope)
    response = api_client.get(f"/api/v1/locker/{locker_id}/commands")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total", "limit", "offset"} <= body.keys()
    assert len(body["items"]) >= 1
    assert body["total"] >= 1

    # 4. Получаем информацию о конкретной команде
    response = api_client.get(f"/api/v1/locker/{locker_id}/commands/{command_id}")
    assert response.status_code == 200
    command_detail = response.json()
    assert command_detail["id"] == command_id

    # 5. Удаляем шкаф
    response = api_client.delete(f"/api/v1/locker/{locker_id}")
    assert response.status_code == 204


def test_unauthorized_access(wait_for_api):
    """Тест: проверка ошибки авторизации"""
    import requests

    from tests.integration.conftest import BASE_URL

    url = f"{BASE_URL}/api/v1/lockers"

    # Без заголовка X-Remote-User
    response = requests.get(url)
    assert response.status_code == 401
    data = response.json()
    assert data["title"] == "Unauthorized"
    assert data["status"] == 401
    assert "Missing X-Remote-User" in data["detail"]


def test_batch_lockers(api_client):
    """Тест: батч-запрос шкафов (полный, view=brief, view=location)"""
    inv_1 = generate_unique_inventory_number()
    inv_2 = generate_unique_inventory_number()

    # 1. Создаём два шкафа с координатами
    payload_1 = {
        "inventory_number": inv_1,
        "location_address": "ул. Карта, 1",
        "location_lat": 55.751244,
        "location_lon": 37.618423,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }
    payload_2 = {
        "inventory_number": inv_2,
        "location_address": "ул. Карта, 2",
        "location_lat": 59.934280,
        "location_lon": 30.335099,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }
    r1 = api_client.post("/api/v1/lockers", json=payload_1)
    assert r1.status_code == 201
    id_1 = r1.json()["id"]

    r2 = api_client.post("/api/v1/lockers", json=payload_2)
    assert r2.status_code == 201
    id_2 = r2.json()["id"]

    # 2. Батч без view — полные данные
    resp = api_client.post("/api/v1/lockers/batch", json={"locker_ids": [id_1, id_2]})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert id_1 in items
    assert id_2 in items
    assert items[id_1]["inventory_number"] == inv_1
    assert items[id_2]["inventory_number"] == inv_2
    assert "connectivity" in items[id_1]  # full locker has connectivity
    assert "door" in items[id_1]
    assert "operational_status" in items[id_1]
    assert "created_at" in items[id_1]

    # 3. Батч (view=brief) — только id + inventory_number
    resp = api_client.post("/api/v1/lockers/batch",
                           json={"locker_ids": [id_1, id_2], "view": "brief"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[id_1] == {"id": id_1, "inventory_number": inv_1}
    assert items[id_2] == {"id": id_2, "inventory_number": inv_2}

    # 4. Батч (view=location) — id + координаты + operational_status
    resp = api_client.post("/api/v1/lockers/batch",
                           json={"locker_ids": [id_1, id_2], "view": "location"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert set(items[id_1].keys()) == {
        "id", "location_lat", "location_lon", "location_address", "operational_status",
    }
    assert items[id_1]["location_lat"] == 55.751244
    assert items[id_1]["location_lon"] == 37.618423
    assert items[id_1]["location_address"] == "ул. Карта, 1"
    assert items[id_1]["operational_status"] == "normal"
    assert items[id_2]["location_address"] == "ул. Карта, 2"
    assert items[id_2]["operational_status"] == "normal"

    # 5. Несуществующий UUID → null
    fake_uuid = "00000000-0000-0000-0000-000000000000"
    resp = api_client.post("/api/v1/lockers/batch", json={"locker_ids": [id_1, fake_uuid]})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[id_1] is not None
    assert items[fake_uuid] is None

    # 6. Несуществующий UUID для view=location тоже даёт null
    resp = api_client.post("/api/v1/lockers/batch",
                           json={"locker_ids": [id_1, fake_uuid], "view": "location"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items[id_1] is not None
    assert items[fake_uuid] is None

    # Cleanup
    api_client.delete(f"/api/v1/locker/{id_1}")
    api_client.delete(f"/api/v1/locker/{id_2}")


def test_lockers_list_view_location_e2e(api_client):
    """Тест: GET /lockers?view=location возвращает только id и координаты"""
    inv_1 = generate_unique_inventory_number()
    inv_2 = generate_unique_inventory_number()

    payload_1 = {
        "inventory_number": inv_1,
        "location_address": "ул. Карта, 1",
        "location_lat": 55.751244,
        "location_lon": 37.618423,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }
    payload_2 = {
        "inventory_number": inv_2,
        "location_address": "ул. Карта, 2",
        "location_lat": 59.934280,
        "location_lon": 30.335099,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    }

    r1 = api_client.post("/api/v1/lockers", json=payload_1)
    assert r1.status_code == 201
    id_1 = r1.json()["id"]

    r2 = api_client.post("/api/v1/lockers", json=payload_2)
    assert r2.status_code == 201
    id_2 = r2.json()["id"]

    try:
        # 1. Сокращенный ответ — только id и координаты
        resp = api_client.get("/api/v1/lockers", params={"view": "location", "limit": 100})
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body

        items_by_id = {item["id"]: item for item in body["items"] if item["id"] in (id_1, id_2)}
        assert id_1 in items_by_id
        assert id_2 in items_by_id

        for item in items_by_id.values():
            assert set(item.keys()) == {
                "id", "location_lat", "location_lon", "location_address", "operational_status",
            }

        assert items_by_id[id_1]["location_lat"] == 55.751244
        assert items_by_id[id_1]["location_lon"] == 37.618423
        assert items_by_id[id_1]["location_address"] == "ул. Карта, 1"
        assert items_by_id[id_1]["operational_status"] == "normal"
        assert items_by_id[id_2]["location_address"] == "ул. Карта, 2"
        assert items_by_id[id_2]["operational_status"] == "normal"

        # 2. Без view — полный объект (регрессия)
        resp = api_client.get("/api/v1/lockers", params={"limit": 100})
        assert resp.status_code == 200
        full_items = {item["id"]: item for item in resp.json()["items"] if item["id"] in (id_1, id_2)}
        assert "inventory_number" in full_items[id_1]
        assert "connectivity" in full_items[id_1]
        assert "metadata" in full_items[id_1]

        # 3. Невалидное значение → 400
        resp = api_client.get("/api/v1/lockers", params={"view": "garbage"})
        assert resp.status_code == 400
    finally:
        api_client.delete(f"/api/v1/locker/{id_1}")
        api_client.delete(f"/api/v1/locker/{id_2}")


def test_last_opened_at_set_on_door_open(api_client):
    """E2E: при выставлении door=open в БД (то, что делает
    LockerRepository.update_door из device_handler по результату
    executed open-команды) поле last_opened_at должно стать non-null
    и корректно сериализоваться в GET locker.

    Тест воспроизводит SQL-запрос update_door(OPEN) напрямую в БД,
    т.к. публичного HTTP-эндпоинта для смены door нет
    (door управляется только через WS device_handler)."""
    inv = generate_unique_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    try:
        # 1. До open: last_opened_at is null
        resp = api_client.get(f"/api/v1/locker/{locker_id}")
        assert resp.status_code == 200
        assert resp.json()["last_opened_at"] is None

        # 2. Имитируем executed open: SQL ровно как в LockerRepository.update_door.
        # who_open_last = NULL имитирует команду без идентификации пользователя
        # (always-overwrite семантика); этот тест проверяет именно last_opened_at,
        # отдельные сценарии по who_open_last лежат в test_who_open_last.py.
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE lockers
                    SET door = 'open',
                        last_opened_at = now(),
                        who_open_last = NULL,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (locker_id,),
                )
            conn.commit()
        finally:
            conn.close()

        # 3. После open: last_opened_at не null и сериализуется в JSON
        resp = api_client.get(f"/api/v1/locker/{locker_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["door"] == "open"
        first_opened = body["last_opened_at"]
        assert first_opened is not None

        # 4. Имитируем close: last_opened_at не должен затереться
        conn = _db_connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE lockers
                    SET door = 'closed',
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (locker_id,),
                )
            conn.commit()
        finally:
            conn.close()

        resp = api_client.get(f"/api/v1/locker/{locker_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["door"] == "closed"
        assert body["last_opened_at"] == first_opened
    finally:
        api_client.delete(f"/api/v1/locker/{locker_id}")


def test_batch_lockers_validation(api_client):
    """Тест: валидация батч-запроса шкафов"""
    # Пустой locker_ids → 400
    resp = api_client.post("/api/v1/lockers/batch", json={"locker_ids": []})
    assert resp.status_code == 400

    # Более 100 → 400
    resp = api_client.post("/api/v1/lockers/batch", json={
        "locker_ids": [f"00000000-0000-0000-0000-{i:012d}" for i in range(101)]
    })
    assert resp.status_code == 400

    # Отсутствует locker_ids → 400
    resp = api_client.post("/api/v1/lockers/batch", json={"view": "brief"})
    assert resp.status_code == 400

    # Невалидный view → 400
    fake_uuid = "00000000-0000-0000-0000-000000000001"
    resp = api_client.post("/api/v1/lockers/batch",
                           json={"locker_ids": [fake_uuid], "view": "garbage"})
    assert resp.status_code == 400


def _create_two_lockers(api_client, address_prefix):
    """Создаёт два шкафа под administrator'ом (дефолтная роль api_client).

    Возвращает (id_a, id_b) — общий сетап для тестов Допуска на список
    (row-scoping GET /api/v1/lockers по X-Remote-Lockers)."""
    resp_a = api_client.post("/api/v1/lockers", json={
        "inventory_number": generate_unique_inventory_number(),
        "location_address": f"{address_prefix}, 1",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp_a.status_code == 201, resp_a.text

    resp_b = api_client.post("/api/v1/lockers", json={
        "inventory_number": generate_unique_inventory_number(),
        "location_address": f"{address_prefix}, 2",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp_b.status_code == 201, resp_b.text

    return resp_a.json()["id"], resp_b.json()["id"]


def test_worker_scope_lockers_list_only_granted(api_client):
    """SEC: worker с Допуском только на locker A не видит locker B в /lockers.

    Регрессионный тест для бага, из-за которого GET /api/v1/lockers отдавал
    worker'у ВСЕ шкафы независимо от per-locker Допуска. total обязан
    считаться по той же области видимости, что и items (SQL-фильтр уходит
    в ОБА запроса — get_all и count) — иначе пагинация "обещает" строки,
    которых на самом деле нет; это критичный ассерт, а не косметика.
    """
    id_a, id_b = _create_two_lockers(api_client, "ул. Скоуп")

    try:
        response = api_client.get(
            "/api/v1/lockers",
            headers={"X-Remote-Role": "worker", "X-Remote-Lockers": id_a},
        )
        assert response.status_code == 200
        body = response.json()
        ids = [item["id"] for item in body["items"]]
        assert id_a in ids
        assert id_b not in ids
        # КРИТИЧНО: область Допуска — ровно [id_a], поэтому total обязан
        # быть равен 1 независимо от того, сколько шкафов лежит в БД
        # от других тестов (id-фильтр детерминирован по конкретному UUID).
        assert body["total"] == 1
    finally:
        api_client.delete(f"/api/v1/locker/{id_a}")
        api_client.delete(f"/api/v1/locker/{id_b}")


def test_worker_scope_lockers_list_empty_grant_returns_empty(api_client):
    """worker без единого Допуска (X-Remote-Lockers: "") получает 200 с
    пустым списком, а НЕ 403 — так и задумано: пустой Допуск не должен
    ломать session-scoped probe (см. wait_for_api в conftest.py, который
    принимает только коды 200/401)."""
    id_a, id_b = _create_two_lockers(api_client, "ул. Скоуп-пусто")

    try:
        response = api_client.get(
            "/api/v1/lockers",
            headers={"X-Remote-Role": "worker", "X-Remote-Lockers": ""},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["items"] == []
        assert body["total"] == 0
    finally:
        api_client.delete(f"/api/v1/locker/{id_a}")
        api_client.delete(f"/api/v1/locker/{id_b}")


def test_administrator_lockers_list_ignores_scope(api_client):
    """Регрессия: administrator не должен пострадать от row-scoping — видит
    оба шкафа независимо от Допуска (роль решает раньше заголовка
    X-Remote-Lockers, см. resolve_locker_scope)."""
    id_a, id_b = _create_two_lockers(api_client, "ул. Скоуп-админ")

    try:
        response = api_client.get("/api/v1/lockers", params={"limit": 1000})
        assert response.status_code == 200
        ids = [item["id"] for item in response.json()["items"]]
        assert id_a in ids
        assert id_b in ids
    finally:
        api_client.delete(f"/api/v1/locker/{id_a}")
        api_client.delete(f"/api/v1/locker/{id_b}")


def test_worker_scope_lockers_list_full_access_marker(api_client):
    """worker с X-Remote-Lockers: "*" (full_access) видит оба шкафа — маркер
    полного доступа снимает row-scoping точно так же, как роль
    operator/administrator (нулевая регрессия на горячем пути)."""
    id_a, id_b = _create_two_lockers(api_client, "ул. Скоуп-фулл")

    try:
        response = api_client.get(
            "/api/v1/lockers",
            headers={"X-Remote-Role": "worker", "X-Remote-Lockers": "*"},
            params={"limit": 1000},
        )
        assert response.status_code == 200
        ids = [item["id"] for item in response.json()["items"]]
        assert id_a in ids
        assert id_b in ids
    finally:
        api_client.delete(f"/api/v1/locker/{id_a}")
        api_client.delete(f"/api/v1/locker/{id_b}")


def test_worker_scope_lockers_list_dashless_uuid_grant(api_client):
    """SEC-02: грант в X-Remote-Lockers в дефис-свободном виде (32 hex-символа
    без дефисов) канонизируется ДО сравнения — worker всё равно видит
    locker A, а не проваливается в пустую область из-за наивного строкового
    сравнения с каноническим (дефисным) UUID в БД."""
    id_a, id_b = _create_two_lockers(api_client, "ул. Скоуп-дэшлесс")
    dashless_a = id_a.replace("-", "")

    try:
        response = api_client.get(
            "/api/v1/lockers",
            headers={"X-Remote-Role": "worker", "X-Remote-Lockers": dashless_a},
        )
        assert response.status_code == 200
        body = response.json()
        ids = [item["id"] for item in body["items"]]
        assert id_a in ids
        assert id_b not in ids
        assert body["total"] == 1
    finally:
        api_client.delete(f"/api/v1/locker/{id_a}")
        api_client.delete(f"/api/v1/locker/{id_b}")
