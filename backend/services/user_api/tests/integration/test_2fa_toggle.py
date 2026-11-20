"""Интеграционные тесты enable-2fa / disable-2fa (реальная БД)."""
import time


def _unique_phone() -> str:
    return f"+7{str(int(time.time() * 1000))[-10:]}"


def _create_user(api_client, *, with_password: bool) -> int:
    payload = {"phone": _unique_phone(), "full_name": "2FA Integration"}
    if with_password:
        payload["password"] = "secretpw123"
    resp = api_client.post("/api/v1/users", json=payload)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_enable_unbound_returns_invite_and_persists(api_client, db_connection):
    uid = _create_user(api_client, with_password=True)
    resp = api_client.post(f"/api/v1/user/{uid}/enable-2fa", json={})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["require_2fa"] is True
    assert body["max_bound"] is False
    assert body["max_invite_token"]

    with db_connection.cursor() as cur:
        cur.execute("SELECT require_2fa FROM user_auth WHERE user_id = %s", (uid,))
        assert cur.fetchone()[0] is True

    api_client.delete(f"/api/v1/user/{uid}")


def test_disable_clears_flag_and_idempotent(api_client, db_connection):
    uid = _create_user(api_client, with_password=True)
    api_client.post(f"/api/v1/user/{uid}/enable-2fa", json={})

    r1 = api_client.post(f"/api/v1/user/{uid}/disable-2fa", json={})
    assert r1.status_code == 204
    with db_connection.cursor() as cur:
        cur.execute("SELECT require_2fa FROM user_auth WHERE user_id = %s", (uid,))
        assert cur.fetchone()[0] is False

    r2 = api_client.post(f"/api/v1/user/{uid}/disable-2fa", json={})
    assert r2.status_code == 204  # идемпотентно

    api_client.delete(f"/api/v1/user/{uid}")


def test_enable_no_password_returns_409(api_client):
    uid = _create_user(api_client, with_password=False)
    resp = api_client.post(f"/api/v1/user/{uid}/enable-2fa", json={})
    assert resp.status_code == 409
    api_client.delete(f"/api/v1/user/{uid}")


def test_enable_missing_user_returns_404(api_client):
    resp = api_client.post("/api/v1/user/99999999/enable-2fa", json={})
    assert resp.status_code == 404
