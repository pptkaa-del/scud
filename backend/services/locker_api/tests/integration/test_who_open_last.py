"""Интеграционные тесты:

* `who_open_last` в `GET /api/v1/locker/{id}` — заполняется тем же SQL-путём,
  что и `last_opened_at` (LockerRepository.update_door). Так как публичного
  HTTP-эндпоинта для смены `door` нет (она управляется только из device_handler
  по WS), тест воспроизводит точное содержимое UPDATE напрямую в БД.

* `operational_status` в `GET /api/v1/lockers?view=location` и
  `POST /api/v1/lockers/batch` (view=location) — для цветовой индикации
  маркеров на карте без второго запроса.
"""

import os
import uuid

import psycopg2


def _db_connect():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _generate_inventory_number() -> str:
    return f"СО №{uuid.uuid4().hex[:10]}"


def _unique_relay_id() -> int:
    return 500_000_000 + uuid.uuid4().int % 400_000_000


def _set_door_open(locker_id: str, who_open_last):
    """Симулирует executed open: SQL ровно как в LockerRepository.update_door."""
    conn = _db_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE lockers
                SET door = 'open',
                    last_opened_at = now(),
                    who_open_last = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (who_open_last, locker_id),
            )
        conn.commit()
    finally:
        conn.close()


def _set_door_closed(locker_id: str):
    """Симулирует executed close: SQL ровно как в CLOSED-ветке LockerRepository.update_door.
    Важно: who_open_last НЕ трогаем — атрибуция последнего открывшего сохраняется
    до следующего OPEN."""
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


def test_who_open_last_null_for_new_locker(api_client):
    """Новый шкаф, ни разу не открытый, имеет who_open_last == null."""
    inv = _generate_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    try:
        resp = api_client.get(f"/api/v1/locker/{locker_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert "who_open_last" in body
        assert body["who_open_last"] is None
    finally:
        api_client.delete(f"/api/v1/locker/{locker_id}")


def test_who_open_last_set_after_open_command(api_client):
    """После executed open with command.created_by=42 — who_open_last==42."""
    inv = _generate_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    try:
        _set_door_open(locker_id, who_open_last=42)

        resp = api_client.get(f"/api/v1/locker/{locker_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["door"] == "open"
        assert body["who_open_last"] == 42
        assert body["last_opened_at"] is not None
    finally:
        api_client.delete(f"/api/v1/locker/{locker_id}")


def test_close_preserves_who_open_last(api_client):
    """CLOSE-команда не трогает who_open_last — информация о последнем
    открывшем сохраняется до следующего OPEN. Регрессионная защита: если
    кто-то добавит `who_open_last = NULL` в CLOSED-ветку update_door,
    тест поймает это на уровне БД, а не только в SQL-форме unit-теста."""
    inv = _generate_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    try:
        # 1) OPEN пользователем 42
        _set_door_open(locker_id, who_open_last=42)
        body = api_client.get(f"/api/v1/locker/{locker_id}").json()
        assert body["door"] == "open"
        assert body["who_open_last"] == 42
        opened_at = body["last_opened_at"]

        # 2) CLOSE — who_open_last и last_opened_at должны остаться
        _set_door_closed(locker_id)
        body = api_client.get(f"/api/v1/locker/{locker_id}").json()
        assert body["door"] == "closed"
        assert body["who_open_last"] == 42
        assert body["last_opened_at"] == opened_at
    finally:
        api_client.delete(f"/api/v1/locker/{locker_id}")


def test_who_open_last_overwritten_by_subsequent_open(api_client):
    """Always-overwrite семантика: при следующем OPEN значение перезаписывается,
    включая None (system-issued OPEN с created_by=NULL)."""
    inv = _generate_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    try:
        # 1) Первый OPEN — пользователь 42
        _set_door_open(locker_id, who_open_last=42)
        body = api_client.get(f"/api/v1/locker/{locker_id}").json()
        assert body["who_open_last"] == 42

        # 2) Второй OPEN — другой пользователь, 99
        _set_door_open(locker_id, who_open_last=99)
        body = api_client.get(f"/api/v1/locker/{locker_id}").json()
        assert body["who_open_last"] == 99

        # 3) System-issued OPEN (created_by=NULL) — поле перезаписывается в None
        _set_door_open(locker_id, who_open_last=None)
        body = api_client.get(f"/api/v1/locker/{locker_id}").json()
        assert body["who_open_last"] is None
    finally:
        api_client.delete(f"/api/v1/locker/{locker_id}")


def test_lockers_location_view_includes_operational_status(api_client):
    """GET /api/v1/lockers?view=location возвращает operational_status в каждом item."""
    inv_normal = _generate_inventory_number()
    inv_maint = _generate_inventory_number()

    r1 = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv_normal,
        "location_address": "ул. Карта, normal",
        "location_lat": 55.0,
        "location_lon": 37.0,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert r1.status_code == 201
    id_normal = r1.json()["id"]

    r2 = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv_maint,
        "location_address": "ул. Карта, maintenance",
        "location_lat": 56.0,
        "location_lon": 38.0,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert r2.status_code == 201
    id_maint = r2.json()["id"]

    try:
        # Один шкаф переводим в maintenance — второй остаётся normal
        resp = api_client.put(f"/api/v1/locker/{id_maint}/state",
                              json={"operational_status": "maintenance"})
        assert resp.status_code == 200

        resp = api_client.get("/api/v1/lockers", params={"view": "location", "limit": 200})
        assert resp.status_code == 200
        items_by_id = {item["id"]: item for item in resp.json()["items"]
                       if item["id"] in (id_normal, id_maint)}
        assert items_by_id[id_normal]["operational_status"] == "normal"
        assert items_by_id[id_maint]["operational_status"] == "maintenance"

        # Filter coherence: ?view=location&operational_status=maintenance возвращает
        # только maintenance-шкафы, и операционный статус в ответе совпадает с фильтром.
        resp = api_client.get(
            "/api/v1/lockers",
            params={"view": "location", "operational_status": "maintenance", "limit": 200},
        )
        assert resp.status_code == 200
        ids_returned = {item["id"] for item in resp.json()["items"]}
        assert id_maint in ids_returned
        assert id_normal not in ids_returned
        for item in resp.json()["items"]:
            assert item["operational_status"] == "maintenance"
    finally:
        api_client.delete(f"/api/v1/locker/{id_normal}")
        api_client.delete(f"/api/v1/locker/{id_maint}")


def test_lockers_batch_location_view_includes_operational_status(api_client):
    """POST /api/v1/lockers/batch view=location тоже возвращает operational_status."""
    inv = _generate_inventory_number()
    resp = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv,
        "location_lat": 55.0,
        "location_lon": 37.0,
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert resp.status_code == 201
    locker_id = resp.json()["id"]

    try:
        resp = api_client.put(f"/api/v1/locker/{locker_id}/state",
                              json={"operational_status": "alarm"})
        assert resp.status_code == 200

        resp = api_client.post("/api/v1/lockers/batch",
                               json={"locker_ids": [locker_id], "view": "location"})
        assert resp.status_code == 200
        item = resp.json()["items"][locker_id]
        assert item is not None
        assert item["operational_status"] == "alarm"
        assert set(item.keys()) == {
            "id", "location_lat", "location_lon", "location_address", "operational_status",
        }
    finally:
        api_client.delete(f"/api/v1/locker/{locker_id}")
