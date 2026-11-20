"""X-Request-ID на шлюзе: проксированный ответ и short-circuit его несут."""
import requests

from conftest import GATEWAY_URL


def test_proxied_response_carries_request_id(admin_session):
    """Реальный проксированный ответ бэкенда несёт X-Request-ID."""
    resp = admin_session.get(f"{GATEWAY_URL}/api/v1/users")
    assert resp.status_code == 200, resp.text
    rid = resp.headers.get("X-Request-ID")
    assert rid, "проксированный ответ должен нести X-Request-ID"
    assert len(rid) >= 8


def test_short_circuit_401_carries_request_id(wait_for_gateway):
    """401 без cookie (mod_lua short-circuit) эхо X-Request-ID."""
    resp = requests.get(f"{GATEWAY_URL}/api/v1/users")
    assert resp.status_code == 401, resp.text
    assert resp.headers.get("X-Request-ID"), "401 short-circuit должен нести X-Request-ID"
