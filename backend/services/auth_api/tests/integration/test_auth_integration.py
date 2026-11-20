"""Интеграционные тесты для auth_api"""
import os

import requests

from tests.integration.conftest import (
    ADMIN_PASSWORD,
    ADMIN_PHONE,
    ADMIN_USER_ID,
    BASE_URL,
    TEST_PASSWORD,
    TEST_PHONE,
    TEST_USER_ID,
    insert_otp,
)

# =============================================================================
# Login (шаг 1)
# =============================================================================


def test_login_valid_credentials_without_max_binding_returns_503(wait_for_api):
    """Корректные учётные данные + require_2fa, но ни один канал не доставил OTP → 503.

    SMS-шлюз в тестовом окружении не настроен, а у тестового пользователя
    max_user_id IS NULL, поэтому notification_api кладёт код в outbox и
    возвращает queued_until_binding. SMS-primary: телефон есть всегда, значит
    queued означает недоступность доставки, а не "нужна Max-привязка" —
    login отвечает 503, а не 401.
    """
    response = requests.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"phone": TEST_PHONE, "password": TEST_PASSWORD},
    )
    assert response.status_code == 503


def test_login_returns_401_for_invalid_credentials(wait_for_api):
    """Неверный пароль → 401."""
    response = requests.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"phone": TEST_PHONE, "password": "wrongpassword"},
    )
    assert response.status_code == 401


def test_login_returns_401_for_unknown_phone(wait_for_api):
    """Несуществующий телефон → 401."""
    response = requests.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"phone": "+70000000000", "password": "any"},
    )
    assert response.status_code == 401


def test_login_missing_fields(api_client):
    """Отсутствие обязательных полей → 400."""
    response = api_client.post("/api/v1/auth/login", json={"phone": TEST_PHONE})
    assert response.status_code == 400

    response = api_client.post("/api/v1/auth/login", json={"password": TEST_PASSWORD})
    assert response.status_code == 400


# =============================================================================
# Verify (шаг 2)
# =============================================================================

def test_verify_with_correct_otp_sets_cookie(wait_for_api):
    """Правильный OTP → 200 + access_token cookie установлен."""
    code = "234567"
    insert_otp(TEST_PHONE, code)

    session = requests.Session()
    response = session.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert response.status_code == 200
    data = response.json()
    assert "message" in data
    assert "access_token" not in data
    # 2FA-путь возвращает имя и роль так же, как login без 2FA.
    assert data["name"] == "Auth Test User"
    assert data["role"] == "worker"
    # Сессионный cookie должен быть установлен
    assert "access_token" in session.cookies


def test_verify_with_wrong_otp_returns_401(wait_for_api):
    """Неверный OTP → 401."""
    code = "345678"
    insert_otp(TEST_PHONE, code)

    response = requests.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": "000000"},
    )
    assert response.status_code == 401
    data = response.json()
    assert data["status"] == 401


def test_verify_missing_fields(api_client):
    """Отсутствие обязательных полей → 400."""
    response = api_client.post("/api/v1/auth/verify", json={"phone": TEST_PHONE})
    assert response.status_code == 400

    response = api_client.post("/api/v1/auth/verify", json={"code": "123456"})
    assert response.status_code == 400


def test_verify_otp_max_attempts(wait_for_api):
    """После 3 неверных попыток → 429 Too Many Requests."""
    code = "456789"
    insert_otp(TEST_PHONE, code)

    for _ in range(3):
        response = requests.post(
            f"{BASE_URL}/api/v1/auth/verify",
            json={"phone": TEST_PHONE, "code": "000000"},
        )
        if response.status_code == 429:
            break
    # Последняя попытка должна вернуть 429
    response = requests.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": "000000"},
    )
    assert response.status_code in (401, 429)  # зависит от порядка


def test_verify_used_otp_cannot_be_reused(wait_for_api):
    """Уже использованный OTP не работает повторно."""
    code = "567890"
    insert_otp(TEST_PHONE, code)

    # Первое использование — успех
    response = requests.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert response.status_code == 200

    # Второе использование — нет активного OTP
    response = requests.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert response.status_code == 401


# =============================================================================
# Validate (internal endpoint)
# =============================================================================

def test_validate_with_valid_session_returns_200(auth_session):
    """Валидный access token → 200 + X-User-Id заголовок."""
    access_token = auth_session.cookies["access_token"]
    response = requests.get(
        f"{BASE_URL}/auth/validate",
        headers={"X-Access-Token": access_token},
    )
    assert response.status_code == 200
    assert "X-User-Id" in response.headers
    assert response.headers["X-User-Id"].isdigit()


def test_validate_without_token_returns_401(wait_for_api):
    """Запрос без токена → 401."""
    response = requests.get(f"{BASE_URL}/auth/validate")
    assert response.status_code == 401


def test_validate_with_invalid_token_returns_401(wait_for_api):
    """Невалидный токен → 401."""
    response = requests.get(
        f"{BASE_URL}/auth/validate",
        headers={"X-Access-Token": "invalid_token_that_does_not_exist"},
    )
    assert response.status_code == 401


# =============================================================================
# Access
# =============================================================================

def test_access_with_valid_cookie_renews_session(wait_for_api):
    """Валидный access cookie → 200 + новый cookie установлен."""
    code = "678901"
    insert_otp(TEST_PHONE, code)

    session = requests.Session()
    verify_response = session.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert verify_response.status_code == 200
    original_cookie = session.cookies["access_token"]

    access_response = session.post(f"{BASE_URL}/api/v1/auth/access")
    assert access_response.status_code == 200
    data = access_response.json()
    assert "message" in data
    assert "access_token" not in data
    # Новый cookie должен отличаться от старого
    assert session.cookies["access_token"] != original_cookie


def test_access_without_cookie_returns_401(wait_for_api):
    """Запрос без access cookie → 401."""
    response = requests.post(f"{BASE_URL}/api/v1/auth/access")
    assert response.status_code == 401


# =============================================================================
# Logout
# =============================================================================

def test_logout_returns_204(auth_session):
    """Logout с валидной сессией → 204."""
    response = auth_session.post(f"{BASE_URL}/api/v1/auth/logout")
    assert response.status_code == 204


def test_logout_revokes_session(wait_for_api):
    """После logout access token больше не проходит validate."""
    code = "789012"
    insert_otp(TEST_PHONE, code)

    session = requests.Session()
    verify_response = session.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert verify_response.status_code == 200
    access_token = session.cookies["access_token"]

    # Токен валиден до logout
    validate_before = requests.get(
        f"{BASE_URL}/auth/validate",
        headers={"X-Access-Token": access_token},
    )
    assert validate_before.status_code == 200

    # Logout
    session.post(f"{BASE_URL}/api/v1/auth/logout")

    # После logout токен отозван → 401
    validate_after = requests.get(
        f"{BASE_URL}/auth/validate",
        headers={"X-Access-Token": access_token},
    )
    assert validate_after.status_code == 401


def test_logout_revokes_access_cookie_for_access(wait_for_api):
    """После logout access cookie не работает для /auth/access."""
    code = "890123"
    insert_otp(TEST_PHONE, code)

    session = requests.Session()
    verify_response = session.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert verify_response.status_code == 200

    # Logout
    session.post(f"{BASE_URL}/api/v1/auth/logout")

    # Access после logout → 401
    access_response = session.post(f"{BASE_URL}/api/v1/auth/access")
    assert access_response.status_code == 401


def test_logout_idempotent(wait_for_api):
    """Повторный logout возвращает 204 (idempotent)."""
    code = "901234"
    insert_otp(TEST_PHONE, code)

    session = requests.Session()
    verify_response = session.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert verify_response.status_code == 200

    for _ in range(2):
        response = session.post(f"{BASE_URL}/api/v1/auth/logout")
        assert response.status_code == 204


def test_logout_without_tokens_returns_204(wait_for_api):
    """Logout без каких-либо токенов → 204 (graceful)."""
    response = requests.post(f"{BASE_URL}/api/v1/auth/logout")
    assert response.status_code == 204


# =============================================================================
# Login без 2FA (require_2fa=False)
# =============================================================================

def test_login_without_2fa_sets_cookie_directly(wait_for_api):
    """Администратор с require_2fa=False → 200 + cookie + name + role сразу без OTP."""
    session = requests.Session()
    response = session.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
    )
    assert response.status_code == 200
    data = response.json()
    assert data.get("message") == "Authentication successful"
    assert data.get("name") == "Auth Test Admin"
    assert data.get("role") == "administrator"
    assert "access_token" in session.cookies


# =============================================================================
# Access: невалидный токен
# =============================================================================

def test_access_with_invalid_token_returns_401(wait_for_api):
    """Cookie есть, но токен не существует в БД → 401."""
    session = requests.Session()
    session.cookies.set("access_token", "nonexistent_token_value_that_is_not_in_db", domain="localhost")
    response = session.post(f"{BASE_URL}/api/v1/auth/access")
    assert response.status_code == 401


# =============================================================================
# Revoke User Tokens (администраторский эндпоинт)
# =============================================================================

def test_revoke_tokens_without_session_returns_401(wait_for_api):
    """Запрос без X-Remote-User → 401 (нет аутентификации)."""
    response = requests.delete(f"{BASE_URL}/api/v1/auth/users/{TEST_USER_ID}/tokens")
    assert response.status_code == 401


def test_revoke_tokens_as_non_admin_returns_403(wait_for_api):
    """Worker пытается отозвать токены → 403 Forbidden."""
    response = requests.delete(
        f"{BASE_URL}/api/v1/auth/users/{TEST_USER_ID}/tokens",
        headers={"X-Remote-User": str(TEST_USER_ID)},
    )
    assert response.status_code == 403
    data = response.json()
    assert data["status"] == 403


def test_revoke_tokens_as_admin_returns_revoked_count(wait_for_api):
    """Администратор отзывает активные токены пользователя → 200 с количеством."""
    # Создаём сессию для тестового пользователя
    code = "112233"
    insert_otp(TEST_PHONE, code)
    user_session = requests.Session()
    verify_response = user_session.post(
        f"{BASE_URL}/api/v1/auth/verify",
        json={"phone": TEST_PHONE, "code": code},
    )
    assert verify_response.status_code == 200

    # Администратор отзывает токены
    response = requests.delete(
        f"{BASE_URL}/api/v1/auth/users/{TEST_USER_ID}/tokens",
        headers={"X-Remote-User": str(ADMIN_USER_ID)},
    )
    assert response.status_code == 200
    data = response.json()
    assert "revoked" in data
    assert data["revoked"] >= 1

    # Токен больше не валиден
    token = user_session.cookies.get("access_token")
    validate_response = requests.get(
        f"{BASE_URL}/auth/validate",
        headers={"X-Access-Token": token},
    )
    assert validate_response.status_code == 401


def test_revoke_tokens_with_no_active_tokens_returns_zero(wait_for_api):
    """Пользователь без активных токенов → 200 с {"revoked": 0}."""
    response = requests.delete(
        f"{BASE_URL}/api/v1/auth/users/{TEST_USER_ID}/tokens",
        headers={"X-Remote-User": str(ADMIN_USER_ID)},
    )
    # Вызываем второй раз подряд — все токены уже отозваны
    response = requests.delete(
        f"{BASE_URL}/api/v1/auth/users/{TEST_USER_ID}/tokens",
        headers={"X-Remote-User": str(ADMIN_USER_ID)},
    )
    assert response.status_code == 200
    assert response.json()["revoked"] == 0


def test_revoke_tokens_invalid_user_id_returns_404(wait_for_api):
    """Нечисловой user_id → Tornado не находит маршрут → 404."""
    response = requests.delete(
        f"{BASE_URL}/api/v1/auth/users/not-a-number/tokens",
        headers={"X-Remote-User": str(ADMIN_USER_ID)},
    )
    assert response.status_code == 404


# =============================================================================
# Rate Limiting (запускается последним — исчерпывает лимит для тестового IP)
# =============================================================================

def test_login_rate_limit_triggers_429(wait_for_api):
    """Многократные неверные попытки логина с одного IP → 429 Too Many Requests."""
    # Лимит задаётся в docker-compose (LOGIN_RATE_LIMIT, dev-дефолт 100 — под
    # объём логинов gateway_tests); читаем его же из env, чтобы тест не
    # рассыхался с конфигом. +5 — предыдущие тесты сьюта уже потратили часть
    # бюджета, 429 должен наступить не позже limit+5 попыток.
    rate_limit = int(os.getenv("LOGIN_RATE_LIMIT", "100"))
    last_resp = None
    for _ in range(rate_limit + 5):
        last_resp = requests.post(
            f"{BASE_URL}/api/v1/auth/login",
            json={"phone": "+79999999999", "password": "wrongpassword"},
        )
        if last_resp.status_code == 429:
            break

    assert last_resp is not None, "no rate-limit response captured"
    assert last_resp.status_code == 429, (
        f"Rate limit (429) never triggered within {rate_limit + 5} attempts"
    )
    assert last_resp.json()["status"] == 429
