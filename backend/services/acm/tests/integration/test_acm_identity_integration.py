"""
Интеграционные тесты GET /acm/identity.

Требуют запущенных сервисов: acm, auth_api, postgres_user, postgres_auth.
Запуск: pytest tests/integration -v

Покрывают:
- Валидный токен → 200 + X-Remote-Role, соответствующий роли посеянного
  пользователя (worker / operator / administrator)
- Отсутствующий токен → 401
- Невалидный токен → 401
- §6.2: ответ не должен содержать check-only заголовки (X-Remote-Lockers,
  X-Remote-Require-2FA) — /acm/identity резолвит только роль, без проверки
  прав по пути
"""
from .conftest import (
    TEST_ADMIN_ID,
    TEST_OPERATOR_ID,
    TEST_WORKER_ID,
    TOKEN_ADMIN,
    TOKEN_OPERATOR,
    TOKEN_WORKER,
)


class TestIdentityTokenValidation:
    def test_missing_token_returns_401(self, client):
        resp = client.identity_public()
        assert resp.status_code == 401

    def test_empty_token_returns_401(self, client):
        resp = client.identity("")
        assert resp.status_code == 401

    def test_invalid_token_returns_401(self, client):
        resp = client.identity("invalid-token-xyz")
        assert resp.status_code == 401


class TestIdentityValidToken:
    def test_worker_token_returns_role(self, client):
        resp = client.identity(TOKEN_WORKER)
        assert resp.status_code == 200
        assert resp.headers.get("X-User-Id") == str(TEST_WORKER_ID)
        assert resp.headers.get("X-Remote-Role") == "worker"

    def test_operator_token_returns_role(self, client):
        resp = client.identity(TOKEN_OPERATOR)
        assert resp.status_code == 200
        assert resp.headers.get("X-User-Id") == str(TEST_OPERATOR_ID)
        assert resp.headers.get("X-Remote-Role") == "operator"

    def test_administrator_token_returns_role(self, client):
        resp = client.identity(TOKEN_ADMIN)
        assert resp.status_code == 200
        assert resp.headers.get("X-User-Id") == str(TEST_ADMIN_ID)
        assert resp.headers.get("X-Remote-Role") == "administrator"

    def test_response_has_no_check_only_headers(self, client):
        # §6.2: /acm/identity резолвит роль без проверки прав по пути —
        # X-Remote-Lockers / X-Remote-Require-2FA принадлежат только /acm/check.
        resp = client.identity(TOKEN_WORKER)
        assert resp.status_code == 200
        assert "X-Remote-Lockers" not in resp.headers
        assert "X-Remote-Require-2FA" not in resp.headers
