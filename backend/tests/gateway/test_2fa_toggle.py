"""Gateway+ACM тесты для enable-2fa / disable-2fa."""
import time

from conftest import GATEWAY_URL


def _unique_phone() -> str:
    return f"+7{str(int(time.time() * 1000))[-10:]}"


def test_worker_denied_enable_2fa(worker_session):
    """Worker → POST /api/v1/user/{id}/enable-2fa → 403."""
    session, user_id = worker_session
    resp = session.post(f"{GATEWAY_URL}/api/v1/user/{user_id}/enable-2fa", json={})
    assert resp.status_code == 403, f"got {resp.status_code}: {resp.text}"


def test_admin_allowed_enable_2fa(admin_session):
    """Administrator → enable-2fa проходит ACM (не 403)."""
    create = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": _unique_phone(), "full_name": "GW 2FA", "password": "secretpw123"},
    )
    assert create.status_code == 201, create.text
    uid = create.json()["id"]
    resp = admin_session.post(f"{GATEWAY_URL}/api/v1/user/{uid}/enable-2fa", json={})
    assert resp.status_code in (200, 204), f"got {resp.status_code}: {resp.text}"
    admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{uid}")
