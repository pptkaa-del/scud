"""
Интеграционные тесты ACM.

Требуют запущенных сервисов: acm, auth_api, postgres_user, postgres_auth.
Запуск: pytest tests/integration -v

Покрывают:
- Публичные пути (без токена)
- Валидация токена (401 при отсутствии / невалидном токене)
- Права по роли: worker / operator / administrator
- Допуск на конкретный шкаф для worker
- Деактивированный пользователь
- Ответные заголовки (X-User-Id, X-Remote-Role, X-Remote-Lockers)
"""
import pytest

from .conftest import (
    TEST_ADMIN_ID,
    TEST_LOCKER_UUID,
    TEST_OPERATOR_ID,
    TEST_WORKER_ID,
    TOKEN_ADMIN,
    TOKEN_DEACTIVATED,
    TOKEN_OPERATOR,
    TOKEN_WORKER,
)

OTHER_LOCKER = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


# ---------------------------------------------------------------------------
# Публичные эндпоинты
# ---------------------------------------------------------------------------

class TestPublicPaths:
    def test_auth_login_no_token_required(self, client):
        resp = client.check_public("POST", "/api/v1/auth/login")
        assert resp.status_code == 200

    def test_auth_verify_no_token_required(self, client):
        resp = client.check_public("POST", "/api/v1/auth/verify")
        assert resp.status_code == 200

    def test_health_no_token_required(self, client):
        resp = client.check_public("GET", "/health")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Валидация токена
# ---------------------------------------------------------------------------

class TestTokenValidation:
    def test_missing_token_returns_401(self, client):
        resp = client.check_public("GET", "/api/v1/users")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, client):
        resp = client.check("invalid-token-xyz", "GET", "/api/v1/users")
        assert resp.status_code == 401

    def test_empty_token_returns_401(self, client):
        resp = client.check("", "GET", "/api/v1/users")
        assert resp.status_code == 401

    def test_valid_token_returns_200(self, client):
        resp = client.check(TOKEN_OPERATOR, "GET", "/api/v1/users")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Ответные заголовки
# ---------------------------------------------------------------------------

class TestResponseHeaders:
    def test_operator_headers(self, client):
        resp = client.check(TOKEN_OPERATOR, "GET", "/api/v1/users")
        assert resp.status_code == 200
        assert resp.headers.get("X-User-Id") == str(TEST_OPERATOR_ID)
        assert resp.headers.get("X-Remote-Role") == "operator"
        assert resp.headers.get("X-Remote-Lockers") == "*"

    def test_administrator_headers(self, client):
        resp = client.check(TOKEN_ADMIN, "GET", "/api/v1/users")
        assert resp.status_code == 200
        assert resp.headers.get("X-User-Id") == str(TEST_ADMIN_ID)
        assert resp.headers.get("X-Remote-Role") == "administrator"
        assert resp.headers.get("X-Remote-Lockers") == "*"

    def test_worker_headers_with_locker(self, client):
        resp = client.check(TOKEN_WORKER, "GET", f"/api/v1/locker/{TEST_LOCKER_UUID}")
        assert resp.status_code == 200
        assert resp.headers.get("X-User-Id") == str(TEST_WORKER_ID)
        assert resp.headers.get("X-Remote-Role") == "worker"
        lockers = resp.headers.get("X-Remote-Lockers", "")
        assert TEST_LOCKER_UUID in lockers


# ---------------------------------------------------------------------------
# Worker — права
# ---------------------------------------------------------------------------

class TestWorkerAccess:
    def test_worker_cannot_list_users(self, client):
        resp = client.check(TOKEN_WORKER, "GET", "/api/v1/users")
        assert resp.status_code == 403

    def test_worker_cannot_create_user(self, client):
        resp = client.check(TOKEN_WORKER, "POST", "/api/v1/users")
        assert resp.status_code == 403

    def test_worker_cannot_list_contractors(self, client):
        resp = client.check(TOKEN_WORKER, "GET", "/api/v1/contractors")
        assert resp.status_code == 403

    def test_worker_can_list_lockers(self, client):
        resp = client.check(TOKEN_WORKER, "GET", "/api/v1/lockers")
        assert resp.status_code == 200

    def test_worker_with_access_can_get_locker(self, client):
        resp = client.check(TOKEN_WORKER, "GET", f"/api/v1/locker/{TEST_LOCKER_UUID}")
        assert resp.status_code == 200

    def test_worker_without_access_denied_other_locker(self, client):
        resp = client.check(TOKEN_WORKER, "GET", f"/api/v1/locker/{OTHER_LOCKER}")
        assert resp.status_code == 403

    def test_worker_with_access_can_send_command(self, client):
        resp = client.check(TOKEN_WORKER, "POST", f"/api/v1/locker/{TEST_LOCKER_UUID}/commands")
        assert resp.status_code == 200

    def test_worker_cannot_send_command_to_other_locker(self, client):
        resp = client.check(TOKEN_WORKER, "POST", f"/api/v1/locker/{OTHER_LOCKER}/commands")
        assert resp.status_code == 403

    def test_worker_cannot_create_access_grant(self, client):
        resp = client.check(TOKEN_WORKER, "POST", "/api/v1/user/1/accesses")
        assert resp.status_code == 403

    def test_worker_cannot_reactivate_user(self, client):
        resp = client.check(TOKEN_WORKER, "POST", "/api/v1/user/1/reactivate")
        assert resp.status_code == 403

    def test_worker_cannot_reactivate_access(self, client):
        resp = client.check(TOKEN_WORKER, "POST", "/api/v1/access/1/reactivate")
        assert resp.status_code == 403

    def test_worker_can_create_emergency_request(self, client):
        resp = client.check(TOKEN_WORKER, "POST", "/api/v1/emergency-requests")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Operator — права
# ---------------------------------------------------------------------------

class TestOperatorAccess:
    def test_operator_can_list_users(self, client):
        resp = client.check(TOKEN_OPERATOR, "GET", "/api/v1/users")
        assert resp.status_code == 200

    def test_operator_can_create_user(self, client):
        resp = client.check(TOKEN_OPERATOR, "POST", "/api/v1/users")
        assert resp.status_code == 200

    def test_operator_can_list_contractors(self, client):
        resp = client.check(TOKEN_OPERATOR, "GET", "/api/v1/contractors")
        assert resp.status_code == 200

    def test_operator_can_get_any_locker(self, client):
        resp = client.check(TOKEN_OPERATOR, "GET", f"/api/v1/locker/{OTHER_LOCKER}")
        assert resp.status_code == 200

    def test_operator_cannot_delete_locker(self, client):
        resp = client.check(TOKEN_OPERATOR, "DELETE", f"/api/v1/locker/{TEST_LOCKER_UUID}")
        assert resp.status_code == 403

    def test_operator_can_manage_accesses(self, client):
        resp = client.check(TOKEN_OPERATOR, "POST", "/api/v1/user/1/accesses")
        assert resp.status_code == 200

    def test_operator_can_reactivate_user(self, client):
        resp = client.check(TOKEN_OPERATOR, "POST", "/api/v1/user/1/reactivate")
        assert resp.status_code == 200

    def test_operator_can_reactivate_access(self, client):
        resp = client.check(TOKEN_OPERATOR, "POST", "/api/v1/access/1/reactivate")
        assert resp.status_code == 200

    def test_operator_can_view_history(self, client):
        resp = client.check(TOKEN_OPERATOR, "GET", "/api/v1/telemetry")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Administrator — права
# ---------------------------------------------------------------------------

class TestAdministratorAccess:
    def test_admin_can_delete_locker(self, client):
        resp = client.check(TOKEN_ADMIN, "DELETE", f"/api/v1/locker/{TEST_LOCKER_UUID}")
        assert resp.status_code == 200

    def test_admin_can_manage_sensor_thresholds(self, client):
        resp = client.check(
            TOKEN_ADMIN, "POST",
            f"/api/v1/locker/{TEST_LOCKER_UUID}/sensors/sensor-uuid/thresholds",
        )
        assert resp.status_code == 200

    def test_admin_can_create_sensor_types(self, client):
        resp = client.check(TOKEN_ADMIN, "POST", "/api/v1/sensor-types")
        assert resp.status_code == 200

    def test_admin_lockers_header_is_wildcard(self, client):
        resp = client.check(TOKEN_ADMIN, "GET", "/api/v1/users")
        assert resp.headers.get("X-Remote-Lockers") == "*"


# ---------------------------------------------------------------------------
# Деактивированный пользователь
# ---------------------------------------------------------------------------

class TestDeactivatedUser:
    def test_deactivated_user_denied(self, client):
        resp = client.check(TOKEN_DEACTIVATED, "GET", "/api/v1/lockers")
        assert resp.status_code == 403

    def test_deactivated_user_denied_even_for_public_looking_path(self, client):
        # /api/v1/lockers — worker-level, но пользователь деактивирован
        resp = client.check(TOKEN_DEACTIVATED, "GET", "/api/v1/lockers")
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class TestHealth:
    def test_health_returns_200_when_db_up(self, client, wait_for_acm):
        resp = client.session.get(f"{client.base_url}/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["db"] == "ok"
