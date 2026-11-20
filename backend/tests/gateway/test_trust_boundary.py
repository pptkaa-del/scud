"""
Trust-boundary тесты для Apache Gateway.

Покрывают регрессии cluster A:
- Подделка X-Remote-User/X-Remote-Role от внешнего клиента не должна давать
  доступа от чужого имени (gateway/Lua обязаны стирать заголовки до проксирования).
- /internal/* эндпойнты должны быть закрыты для внешнего вызова, даже если
  прислать заголовок X-Remote-User.

Все запросы идут через apache_gateway:80. Все 6 backend-сервисов в production
docker-compose.yaml не публикуют свои порты на хост — единственный путь снаружи
лежит через эту точку входа.
"""
import requests

from conftest import GATEWAY_URL


# =============================================================================
# Подделка X-Remote-User / X-Remote-Role
# =============================================================================

class TestRemoteUserSpoofing:
    """Lua hook (gateway/lua/jwt_auth.lua:17-19) обязан стирать X-Remote-*."""

    def test_x_remote_user_spoof_without_cookie_rejected(self, wait_for_gateway):
        """X-Remote-User без access_token cookie → 401 (не пройти ACM)."""
        resp = requests.get(
            f"{GATEWAY_URL}/api/v1/users",
            headers={"X-Remote-User": "9999"},
        )
        assert resp.status_code == 401, (
            f"Spoofed X-Remote-User without cookie должен давать 401, "
            f"получено {resp.status_code}: {resp.text}"
        )

    def test_x_remote_role_spoof_without_cookie_rejected(self, wait_for_gateway):
        """X-Remote-Role: administrator без cookie → 401."""
        resp = requests.get(
            f"{GATEWAY_URL}/api/v1/users",
            headers={"X-Remote-Role": "administrator"},
        )
        assert resp.status_code == 401

    def test_x_remote_user_and_role_spoof_without_cookie_rejected(self, wait_for_gateway):
        """Полный набор поддельных X-Remote-* без cookie → 401."""
        resp = requests.get(
            f"{GATEWAY_URL}/api/v1/users",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "administrator",
                "X-Remote-Lockers": "*",
            },
        )
        assert resp.status_code == 401

    def test_x_remote_role_spoof_with_admin_cookie_uses_real_role(
        self, admin_session
    ):
        """С admin-cookie + X-Remote-Role: worker → запрос на admin-only пройдёт.
        Доказывает: ACM использует реальную роль из БД, а не подделанный заголовок.
        """
        resp = admin_session.get(
            f"{GATEWAY_URL}/api/v1/users",
            headers={"X-Remote-Role": "worker"},
        )
        assert resp.status_code == 200, (
            f"Admin-cookie должна давать админский доступ независимо от поддельного "
            f"X-Remote-Role, получено {resp.status_code}: {resp.text}"
        )

    def test_worker_cookie_with_admin_role_spoof_still_denied(
        self, worker_session
    ):
        """С worker-cookie + X-Remote-Role: administrator → admin-only по-прежнему 403.
        Подделанный заголовок не должен повышать привилегии.
        """
        session, _ = worker_session
        resp = session.get(
            f"{GATEWAY_URL}/api/v1/users",
            headers={"X-Remote-Role": "administrator"},
        )
        assert resp.status_code == 403, (
            f"Worker с поддельным X-Remote-Role admin не должен иметь admin-доступ, "
            f"получено {resp.status_code}: {resp.text}"
        )


# =============================================================================
# /internal/* закрыт снаружи
# =============================================================================

class TestInternalPathsBlocked:
    """apache.conf <Location /internal> Require all denied — внешний доступ запрещён."""

    INTERNAL_PATHS = [
        "/internal/api/v1/events/security",
        "/internal/api/v1/notifications",
        "/internal/api/v1/notification/00000000-0000-0000-0000-000000000000",
        "/internal/whatever",
    ]

    def test_internal_post_blocked(self, wait_for_gateway):
        """POST на /internal/* от внешнего клиента → 403/404."""
        resp = requests.post(
            f"{GATEWAY_URL}/internal/api/v1/events/security",
            json={
                "locker_id": "00000000-0000-0000-0000-000000000000",
                "event_type": "threshold_violation",
                "severity": "high",
            },
        )
        assert resp.status_code in (403, 404), (
            f"/internal POST должен быть закрыт извне, получено {resp.status_code}"
        )

    def test_internal_get_blocked(self, wait_for_gateway):
        """GET на /internal/* → 403/404."""
        resp = requests.get(f"{GATEWAY_URL}/internal/api/v1/notifications")
        assert resp.status_code in (403, 404)

    def test_internal_delete_blocked(self, wait_for_gateway):
        """DELETE на /internal/* → 403/404."""
        resp = requests.delete(
            f"{GATEWAY_URL}/internal/api/v1/notification/"
            "00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code in (403, 404)

    def test_internal_with_x_remote_user_still_blocked(self, wait_for_gateway):
        """Подделка X-Remote-User не открывает /internal/*."""
        resp = requests.post(
            f"{GATEWAY_URL}/internal/api/v1/events/security",
            headers={"X-Remote-User": "1"},
            json={
                "locker_id": "00000000-0000-0000-0000-000000000000",
                "event_type": "threshold_violation",
                "severity": "high",
            },
        )
        assert resp.status_code in (403, 404)

    def test_internal_with_admin_cookie_still_blocked(self, admin_session):
        """Даже с админской cookie — /internal/* не доступен через gateway."""
        resp = admin_session.post(
            f"{GATEWAY_URL}/internal/api/v1/events/security",
            json={
                "locker_id": "00000000-0000-0000-0000-000000000000",
                "event_type": "threshold_violation",
                "severity": "high",
            },
        )
        assert resp.status_code in (403, 404), (
            f"/internal должен быть закрыт даже для админа через gateway, "
            f"получено {resp.status_code}: {resp.text}"
        )
