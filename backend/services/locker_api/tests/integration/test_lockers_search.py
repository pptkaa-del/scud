"""Интеграционные тесты: GET /api/v1/lockers?q= — поиск по inventory_number или address.

Сидим 3 шкафа с разными inventory_number / location_address, прогоняем 5 запросов:
- ABC      → A только
- Ленина   → A + B (по адресу)
- мира     → C (case-insensitive)
- (пусто)  → все 3 (q игнорируется)
- %        → 0 (литерал `%` экранируется, не превращается в wildcard)
"""
import uuid


def _unique_inv(prefix: str) -> str:
    """Уникальный inventory_number с заданным префиксом — чтобы избежать
    коллизий с предыдущими тестовыми прогонами на той же БД."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _unique_relay_id() -> int:
    return 500_000_000 + uuid.uuid4().int % 400_000_000


def test_search_lockers_by_q(api_client):
    """Полный сценарий поиска по q со всеми граничными ветками."""
    inv_a = _unique_inv("ABC")
    inv_b = _unique_inv("XYZ")
    inv_c = _unique_inv("ZZZ")

    # Шкаф A — найдётся по inventory_number=ABC и по address=Ленина
    r_a = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv_a,
        "location_address": "ул. Ленина, 1",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert r_a.status_code == 201, r_a.text
    id_a = r_a.json()["id"]

    # Шкаф B — найдётся только по address=Ленина
    r_b = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv_b,
        "location_address": "ул. Ленина, 2",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert r_b.status_code == 201, r_b.text
    id_b = r_b.json()["id"]

    # Шкаф C — найдётся только по address=Мира (case-insensitive `мира`)
    r_c = api_client.post("/api/v1/lockers", json={
        "inventory_number": inv_c,
        "location_address": "пр. Мира, 5",
        "transport": "mqtt",
        "relay_id": _unique_relay_id(),
    })
    assert r_c.status_code == 201, r_c.text
    id_c = r_c.json()["id"]

    try:
        # 1. q=<inv_a первые 3 символа "ABC"> → только A.
        # (Другие шкафы из чужих тестов могут содержать ABC, поэтому
        # фильтруем результат по нашим 3 ID).
        resp = api_client.get("/api/v1/lockers", params={"q": "ABC", "limit": 1000})
        assert resp.status_code == 200
        ids = {itm["id"] for itm in resp.json()["items"]}
        ours = ids & {id_a, id_b, id_c}
        assert ours == {id_a}, f"expected only A, got {ours}"

        # 2. q=Ленина → A и B (поиск по location_address).
        resp = api_client.get("/api/v1/lockers", params={"q": "Ленина", "limit": 1000})
        assert resp.status_code == 200
        ids = {itm["id"] for itm in resp.json()["items"]}
        ours = ids & {id_a, id_b, id_c}
        assert ours == {id_a, id_b}, f"expected A+B, got {ours}"

        # 3. q=мира (нижний регистр) → C (ILIKE регистронезависим).
        resp = api_client.get("/api/v1/lockers", params={"q": "мира", "limit": 1000})
        assert resp.status_code == 200
        ids = {itm["id"] for itm in resp.json()["items"]}
        ours = ids & {id_a, id_b, id_c}
        assert ours == {id_c}, f"expected only C, got {ours}"

        # 4. q= (пусто) → q трактуется как отсутствующий, все 3 присутствуют.
        resp = api_client.get("/api/v1/lockers", params={"q": "", "limit": 1000})
        assert resp.status_code == 200
        ids = {itm["id"] for itm in resp.json()["items"]}
        assert {id_a, id_b, id_c} <= ids

        # 5. q=% → 0 (метасимвол `%` экранируется и ищется как литерал;
        # ни один из наших шкафов не содержит литеральный `%`).
        resp = api_client.get("/api/v1/lockers", params={"q": "%", "limit": 1000})
        assert resp.status_code == 200
        ids = {itm["id"] for itm in resp.json()["items"]}
        ours = ids & {id_a, id_b, id_c}
        assert ours == set(), f"expected no matches for literal `%`, got {ours}"

        # 6. total в paginated response согласован с items при фильтре.
        # Запрашиваем q=Ленина с маленьким limit — total должен учесть фильтр,
        # а не вернуть total всех активных шкафов в БД.
        resp = api_client.get("/api/v1/lockers", params={"q": inv_a, "limit": 1000})
        assert resp.status_code == 200
        body = resp.json()
        # При уникальном инвентарном номере ровно 1 совпадение → total = 1.
        assert body["total"] == 1
        assert len(body["items"]) == 1
        assert body["items"][0]["id"] == id_a
    finally:
        api_client.delete(f"/api/v1/locker/{id_a}")
        api_client.delete(f"/api/v1/locker/{id_b}")
        api_client.delete(f"/api/v1/locker/{id_c}")


def test_search_q_too_long_returns_400(api_client):
    """q длиннее 100 символов → 400 ProblemDetails."""
    long_q = "x" * 101
    resp = api_client.get("/api/v1/lockers", params={"q": long_q})
    assert resp.status_code == 400
    data = resp.json()
    assert data["status"] == 400
    assert "q" in data["detail"]
