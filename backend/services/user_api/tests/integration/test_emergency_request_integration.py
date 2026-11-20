"""Интеграционные тесты для API заявок на экстренное открытие шкафов"""

LOCKER_UUID_1 = "550e8400-e29b-41d4-a716-446655440001"
LOCKER_UUID_2 = "550e8400-e29b-41d4-a716-446655440002"
LOCKER_UUID_3 = "550e8400-e29b-41d4-a716-446655440003"
LOCKER_UUID_4 = "550e8400-e29b-41d4-a716-446655440004"
LOCKER_UUID_5 = "550e8400-e29b-41d4-a716-446655440005"
LOCKER_UUID_6 = "550e8400-e29b-41d4-a716-446655440006"
LOCKER_UUID_7 = "550e8400-e29b-41d4-a716-446655440007"
LOCKER_UUID_8 = "550e8400-e29b-41d4-a716-446655440008"
LOCKER_UUID_9 = "550e8400-e29b-41d4-a716-446655440009"
LOCKER_UUID_10 = "550e8400-e29b-41d4-a716-44665544000a"
LOCKER_UUID_11 = "550e8400-e29b-41d4-a716-44665544000b"
LOCKER_UUID_12 = "550e8400-e29b-41d4-a716-44665544000c"
LOCKER_UUID_13 = "550e8400-e29b-41d4-a716-44665544000d"


def test_create_emergency_request(api_client):
    """Тест: создание заявки на экстренное открытие"""
    payload = {"locker_id": LOCKER_UUID_1}
    response = api_client.post("/api/v1/emergency-requests", json=payload)

    assert response.status_code == 201
    data = response.json()
    assert data["locker_id"] == LOCKER_UUID_1
    assert data["status"] == "pending"
    assert "id" in data
    assert "user_id" in data
    assert "user_full_name" in data
    assert "created_at" in data


def test_create_emergency_request_invalid_locker_id(api_client):
    """Тест: создание заявки с невалидным locker_id (не UUID)"""
    # Строка не в формате UUID
    payload = {"locker_id": "not-a-valid-uuid"}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 400
    assert "locker_id" in response.json()["detail"]

    # Короткая строка, не UUID
    payload = {"locker_id": "12345"}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 400

    # Произвольная строка
    payload = {"locker_id": "abc"}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 400


def test_emergency_request_lifecycle(api_client):
    """Тест: полный жизненный цикл заявки (создание → просмотр → закрытие)"""
    # 1. Создаем заявку
    create_payload = {"locker_id": LOCKER_UUID_2}
    response = api_client.post("/api/v1/emergency-requests", json=create_payload)
    assert response.status_code == 201
    request_id = response.json()["id"]

    # 2. Получаем детальную информацию о заявке
    response = api_client.get(f"/api/v1/emergency-request/{request_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["id"] == request_id
    assert data["locker_id"] == LOCKER_UUID_2
    assert data["status"] == "pending"
    assert data["closed_at"] is None
    assert data["closed_by"] is None

    # 3. Проверяем логи (должно быть 2 записи: created и viewed)
    response = api_client.get(f"/api/v1/emergency-request/{request_id}/logs")
    assert response.status_code == 200
    logs = response.json()
    assert len(logs) >= 2  # created + viewed
    actions = [log["action"] for log in logs]
    assert "created" in actions
    assert "viewed" in actions

    # 4. Закрываем заявку
    response = api_client.delete(f"/api/v1/emergency-request/{request_id}")
    assert response.status_code == 204

    # 5. Проверяем, что заявка закрыта
    response = api_client.get(f"/api/v1/emergency-request/{request_id}")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "closed"
    assert data["closed_at"] is not None
    assert data["closed_by"] is not None

    # 6. Пытаемся закрыть повторно (должна быть ошибка 409)
    response = api_client.delete(f"/api/v1/emergency-request/{request_id}")
    assert response.status_code == 409
    assert "already closed" in response.json()["detail"].lower()

    # 7. Проверяем логи (должно быть 3 записи: created, viewed, closed)
    response = api_client.get(f"/api/v1/emergency-request/{request_id}/logs")
    assert response.status_code == 200
    logs = response.json()
    actions = [log["action"] for log in logs]
    assert "created" in actions
    assert "closed" in actions


def test_get_emergency_requests_list(api_client):
    """Тест: получение списка заявок"""
    # Создаем несколько заявок
    for locker_uuid in [LOCKER_UUID_3, LOCKER_UUID_4, LOCKER_UUID_5]:
        payload = {"locker_id": locker_uuid}
        response = api_client.post("/api/v1/emergency-requests", json=payload)
        assert response.status_code == 201

    # Получаем список всех заявок
    response = api_client.get("/api/v1/emergency-requests")
    assert response.status_code == 200
    data = response.json()
    assert "items" in data
    assert "total" in data
    assert "limit" in data
    assert "offset" in data
    assert len(data["items"]) >= 3  # Хотя бы 3 созданные нами
    assert data["total"] >= 3


def test_get_emergency_requests_with_status_filter(api_client):
    """Тест: фильтрация заявок по статусу"""
    # Создаем заявку
    payload = {"locker_id": LOCKER_UUID_6}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 201
    pending_request_id = response.json()["id"]

    # Создаем еще одну заявку и сразу закрываем её
    payload = {"locker_id": LOCKER_UUID_7}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 201
    closed_request_id = response.json()["id"]
    response = api_client.delete(f"/api/v1/emergency-request/{closed_request_id}")
    assert response.status_code == 204

    # Фильтр по статусу "pending"
    response = api_client.get("/api/v1/emergency-requests?status=pending")
    assert response.status_code == 200
    data = response.json()
    assert all(item["status"] == "pending" for item in data["items"])
    # Проверяем, что наша pending заявка есть в списке
    assert any(item["id"] == pending_request_id for item in data["items"])

    # Фильтр по статусу "closed"
    response = api_client.get("/api/v1/emergency-requests?status=closed")
    assert response.status_code == 200
    data = response.json()
    assert all(item["status"] == "closed" for item in data["items"])
    # Проверяем, что наша closed заявка есть в списке
    assert any(item["id"] == closed_request_id for item in data["items"])


def test_get_emergency_requests_with_locker_filter(api_client):
    """Тест: фильтрация заявок по UUID шкафа"""
    # Создаем заявки для одного шкафа
    locker_500_requests = []
    for _ in range(2):
        payload = {"locker_id": LOCKER_UUID_8}
        response = api_client.post("/api/v1/emergency-requests", json=payload)
        assert response.status_code == 201
        locker_500_requests.append(response.json()["id"])

    payload = {"locker_id": LOCKER_UUID_9}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 201

    # Фильтр по locker_id=LOCKER_UUID_8
    response = api_client.get(f"/api/v1/emergency-requests?locker_id={LOCKER_UUID_8}")
    assert response.status_code == 200
    data = response.json()
    assert all(item["locker_id"] == LOCKER_UUID_8 for item in data["items"])
    assert len(data["items"]) >= 2


def test_emergency_requests_pagination(api_client):
    """Тест: пагинация списка заявок"""
    # Создаем несколько заявок
    pagination_uuids = [LOCKER_UUID_10, LOCKER_UUID_11, LOCKER_UUID_12,
                        "550e8400-e29b-41d4-a716-44665544000e",
                        "550e8400-e29b-41d4-a716-44665544000f"]
    for locker_uuid in pagination_uuids:
        payload = {"locker_id": locker_uuid}
        response = api_client.post("/api/v1/emergency-requests", json=payload)
        assert response.status_code == 201

    # Первая страница (limit=2, offset=0)
    response = api_client.get("/api/v1/emergency-requests?limit=2&offset=0")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 2
    assert data["offset"] == 0
    assert len(data["items"]) <= 2

    # Вторая страница (limit=2, offset=2)
    response = api_client.get("/api/v1/emergency-requests?limit=2&offset=2")
    assert response.status_code == 200
    data = response.json()
    assert data["limit"] == 2
    assert data["offset"] == 2


def test_get_emergency_request_not_found(api_client):
    """Тест: получение несуществующей заявки"""
    response = api_client.get("/api/v1/emergency-request/99999")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_close_emergency_request_not_found(api_client):
    """Тест: закрытие несуществующей заявки"""
    response = api_client.delete("/api/v1/emergency-request/99999")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_get_logs_for_nonexistent_request(api_client):
    """Тест: получение логов для несуществующей заявки"""
    response = api_client.get("/api/v1/emergency-request/99999/logs")
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_emergency_request_details_include_user_info(api_client):
    """Тест: детали заявки включают информацию о пользователе"""
    # Создаем заявку
    payload = {"locker_id": LOCKER_UUID_13}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 201
    request_id = response.json()["id"]

    # Получаем детали
    response = api_client.get(f"/api/v1/emergency-request/{request_id}")
    assert response.status_code == 200
    data = response.json()

    # Проверяем наличие данных о пользователе
    assert "user_id" in data
    assert "user_full_name" in data
    assert "user_phone" in data
    assert data["user_full_name"]  # Не пустое
    assert data["user_phone"]  # Не пустое


def test_emergency_request_invalid_pagination_params(api_client):
    """Тест: невалидные параметры пагинации"""
    # Отрицательный limit
    response = api_client.get("/api/v1/emergency-requests?limit=-1")
    assert response.status_code == 400

    # Слишком большой limit
    response = api_client.get("/api/v1/emergency-requests?limit=10000")
    assert response.status_code == 400

    # Отрицательный offset
    response = api_client.get("/api/v1/emergency-requests?offset=-1")
    assert response.status_code == 400


def test_create_emergency_request_missing_locker_id(api_client):
    """Тест: создание заявки без locker_id"""
    payload = {}
    response = api_client.post("/api/v1/emergency-requests", json=payload)
    assert response.status_code == 400
    assert "locker_id" in response.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Авторизация worker'а (SEC-08/09)
# ---------------------------------------------------------------------------

WORKER_ID = 9001
OTHER_WORKER_ID = 9002
WORKER_GRANTED_LOCKER = "550e8400-e29b-41d4-a716-446655449001"
WORKER_FOREIGN_LOCKER = "550e8400-e29b-41d4-a716-446655449002"


def _seed_worker_with_grant(db_connection):
    """Создаёт worker'а WORKER_ID с активным Допуском на WORKER_GRANTED_LOCKER
    и второго worker'а OTHER_WORKER_ID без Допусков. Идемпотентно."""
    with db_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (id, phone, full_name, state, role)
            VALUES (%s, %s, 'Worker With Grant', 'active', 'worker'),
                   (%s, %s, 'Worker No Grant', 'active', 'worker')
            ON CONFLICT (id) DO UPDATE SET role = EXCLUDED.role, state = EXCLUDED.state
            """,
            (WORKER_ID, "+79009001001", OTHER_WORKER_ID, "+79009002002"),
        )
        cur.execute("SELECT setval('users_id_seq', (SELECT MAX(id) FROM users))")
        # Чистим прежние Допуски этого worker'а, чтобы тест был идемпотентным.
        cur.execute("DELETE FROM user_accesses WHERE user_id = %s", (WORKER_ID,))
        cur.execute(
            """
            INSERT INTO user_accesses (user_id, start_at, granted_by, full_access, is_active)
            VALUES (%s, now() - interval '1 hour', 1, false, true)
            RETURNING id
            """,
            (WORKER_ID,),
        )
        access_id = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO user_access_lockers (user_access_id, locker_id) VALUES (%s, %s)",
            (access_id, WORKER_GRANTED_LOCKER),
        )
        db_connection.commit()


def test_worker_create_without_grant_forbidden(api_client, db_connection):
    """SEC-08: worker без Допуска на шкаф не может подать заявку → 403."""
    _seed_worker_with_grant(db_connection)
    response = api_client.post(
        "/api/v1/emergency-requests",
        json={"locker_id": WORKER_FOREIGN_LOCKER},
        headers={"X-Remote-User": str(WORKER_ID)},
    )
    assert response.status_code == 403


def test_worker_create_with_grant_succeeds(api_client, db_connection):
    """SEC-08: worker с активным Допуском на шкаф подаёт заявку → 201."""
    _seed_worker_with_grant(db_connection)
    response = api_client.post(
        "/api/v1/emergency-requests",
        json={"locker_id": WORKER_GRANTED_LOCKER},
        headers={"X-Remote-User": str(WORKER_ID)},
    )
    assert response.status_code == 201
    assert response.json()["locker_id"] == WORKER_GRANTED_LOCKER


def test_worker_list_scoped_to_own_requests(api_client, db_connection):
    """SEC-09: worker видит в списке только свои заявки."""
    _seed_worker_with_grant(db_connection)
    # Заявка от worker'а с Допуском.
    own = api_client.post(
        "/api/v1/emergency-requests",
        json={"locker_id": WORKER_GRANTED_LOCKER},
        headers={"X-Remote-User": str(WORKER_ID)},
    )
    assert own.status_code == 201
    own_id = own.json()["id"]

    # Заявка от администратора (чужая для worker'а).
    foreign = api_client.post(
        "/api/v1/emergency-requests", json={"locker_id": WORKER_FOREIGN_LOCKER}
    )
    assert foreign.status_code == 201
    foreign_id = foreign.json()["id"]

    response = api_client.get(
        "/api/v1/emergency-requests", headers={"X-Remote-User": str(WORKER_ID)}
    )
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["items"]}
    assert own_id in ids
    assert foreign_id not in ids
    assert all(item["user_id"] == WORKER_ID for item in response.json()["items"])


def test_worker_cannot_read_foreign_request(api_client, db_connection):
    """SEC-09: worker не может прочитать чужую заявку → 404."""
    _seed_worker_with_grant(db_connection)
    foreign = api_client.post(
        "/api/v1/emergency-requests", json={"locker_id": WORKER_FOREIGN_LOCKER}
    )
    assert foreign.status_code == 201
    foreign_id = foreign.json()["id"]

    response = api_client.get(
        f"/api/v1/emergency-request/{foreign_id}",
        headers={"X-Remote-User": str(WORKER_ID)},
    )
    assert response.status_code == 404


def test_worker_can_read_own_request(api_client, db_connection):
    """SEC-09: worker читает свою заявку → 200."""
    _seed_worker_with_grant(db_connection)
    own = api_client.post(
        "/api/v1/emergency-requests",
        json={"locker_id": WORKER_GRANTED_LOCKER},
        headers={"X-Remote-User": str(WORKER_ID)},
    )
    assert own.status_code == 201
    own_id = own.json()["id"]

    response = api_client.get(
        f"/api/v1/emergency-request/{own_id}",
        headers={"X-Remote-User": str(WORKER_ID)},
    )
    assert response.status_code == 200
    assert response.json()["id"] == own_id


def test_operator_sees_all_requests(api_client, db_connection):
    """SEC-09: администратор/оператор видит все заявки (не отскоуплен)."""
    _seed_worker_with_grant(db_connection)
    worker_req = api_client.post(
        "/api/v1/emergency-requests",
        json={"locker_id": WORKER_GRANTED_LOCKER},
        headers={"X-Remote-User": str(WORKER_ID)},
    )
    assert worker_req.status_code == 201
    worker_req_id = worker_req.json()["id"]

    # Администратор (api_client по умолчанию) читает чужую заявку и видит её в списке.
    detail = api_client.get(f"/api/v1/emergency-request/{worker_req_id}")
    assert detail.status_code == 200

    listing = api_client.get("/api/v1/emergency-requests?limit=1000")
    assert listing.status_code == 200
    ids = {item["id"] for item in listing.json()["items"]}
    assert worker_req_id in ids
