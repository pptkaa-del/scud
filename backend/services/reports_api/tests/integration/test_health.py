"""Интеграционный тест health-depth reports_api: /health + /live + /ready (db-only)."""


def test_health_endpoint(api_client):
    resp = api_client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


def test_live_endpoint(api_client):
    resp = api_client.get("/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_ready_endpoint(api_client):
    resp = api_client.get("/ready")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert "redis" not in body
    assert "mqtt" not in body
