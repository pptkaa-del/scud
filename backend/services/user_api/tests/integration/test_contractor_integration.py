"""Интеграционные тесты для API подрядчиков"""


def test_contractor_lifecycle(api_client):
    """Проверка полного цикла CRUD для подрядчика"""
    # 1. Создаем подрядчика
    create_payload = {"name": "Integration Test Contractor"}
    response = api_client.post("/api/v1/contractors", json=create_payload)
    assert response.status_code == 201
    contractor = response.json()
    assert contractor["name"] == "Integration Test Contractor"
    contractor_id = contractor["id"]

    # 2. Получаем подрядчика по ID
    response = api_client.get(f"/api/v1/contractor/{contractor_id}")
    assert response.status_code == 200
    retrieved = response.json()
    assert retrieved["id"] == contractor_id
    assert retrieved["name"] == "Integration Test Contractor"

    # 3. Обновляем подрядчика
    update_payload = {"name": "Updated Contractor Name"}
    response = api_client.put(f"/api/v1/contractor/{contractor_id}", json=update_payload)
    assert response.status_code == 200
    updated = response.json()
    assert updated["name"] == "Updated Contractor Name"

    # 4. Получаем список всех подрядчиков (paginated envelope)
    response = api_client.get("/api/v1/contractors")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total", "limit", "offset"} <= body.keys()
    assert any(c["id"] == contractor_id for c in body["items"])

    # 5. Удаляем подрядчика
    response = api_client.delete(f"/api/v1/contractor/{contractor_id}")
    assert response.status_code == 204

    # 6. Проверяем, что подрядчик удален
    response = api_client.get(f"/api/v1/contractor/{contractor_id}")
    assert response.status_code == 404


def test_get_contractors_list(api_client):
    """Список подрядчиков возвращается в формате PaginatedResponse."""
    response = api_client.get("/api/v1/contractors?limit=10&offset=0")
    assert response.status_code == 200
    body = response.json()
    assert {"items", "total", "limit", "offset"} <= body.keys()
    assert isinstance(body["items"], list)
    assert body["limit"] == 10
    assert body["offset"] == 0
    assert body["total"] >= len(body["items"])


def test_contractor_search_by_name(api_client):
    """`?q=` фильтрует подрядчиков по подстроке имени, нечувствительно к регистру."""
    # Готовим двух подрядчиков с гарантированно различающимися именами.
    target = api_client.post(
        "/api/v1/contractors", json={"name": "Поиск-Цель ABC"}
    ).json()
    other = api_client.post(
        "/api/v1/contractors", json={"name": "Другая Компания"}
    ).json()

    try:
        # Поиск по уникальной подстроке возвращает только целевого.
        response = api_client.get("/api/v1/contractors?q=Цель")
        assert response.status_code == 200
        body = response.json()
        ids = [c["id"] for c in body["items"]]
        assert target["id"] in ids
        assert other["id"] not in ids
        assert body["total"] >= 1

        # Case-insensitive: lowercase подстрока тоже находит.
        response = api_client.get("/api/v1/contractors?q=цель")
        assert response.status_code == 200
        ids_ci = [c["id"] for c in response.json()["items"]]
        assert target["id"] in ids_ci
    finally:
        api_client.delete(f"/api/v1/contractor/{target['id']}")
        api_client.delete(f"/api/v1/contractor/{other['id']}")
