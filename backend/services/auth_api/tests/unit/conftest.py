"""Общие fixtures для unit-тестов auth_api.

Предоставляет:
- `_FakeApp` / `_FakeRequest` — заглушки tornado.web.Application и HTTPServerRequest
- `_make_handler(handler_cls, settings)` — создаёт handler в обход __init__
- `make_rate_limit_repo(check_result)` — мок RateLimitRepository
- `make_otp(...)`, `make_token(...)` — фабрики dataclass-моделей

Эти helpers используются всеми тестовыми модулями (test_otp_lifecycle,
test_token_rotation, test_rate_limit, test_hash_token).

тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

from app.auth_data.db.models.tokens import AccessToken, OtpCode

# ---------------------------------------------------------------------------
# Tornado stand-ins
# ---------------------------------------------------------------------------


class _FakeApp:
    """Минимальный заменитель tornado.web.Application.

    Tornado RequestHandler.settings — read-only property, возвращающее
    self.application.settings, поэтому достаточно подставить fake application
    с нужным dict.
    """

    def __init__(self, settings: dict) -> None:
        self.settings = settings
        self.ui_methods: dict = {}
        self.ui_modules: dict = {}


class _FakeHeaders(dict):
    """dict-like с регистронезависимым `.get` (как HTTPHeaders)."""

    def get(self, key: str, default: Any = None) -> Any:  # type: ignore[override]
        for k, v in self.items():
            if k.lower() == key.lower():
                return v
        return default


class _FakeRequest:
    """Заменитель tornado.httputil.HTTPServerRequest.

    Содержит только поля, которые читают handler'ы:
    `remote_ip`, `headers`, `method`, `uri`, `body`.
    """

    def __init__(
        self,
        remote_ip: str = "127.0.0.1",
        headers: dict | None = None,
        body: bytes = b"",
        method: str = "POST",
        uri: str = "/api/v1/auth/login",
        cookies: dict | None = None,
    ) -> None:
        self.remote_ip = remote_ip
        self.headers = _FakeHeaders(headers or {})
        self.body = body
        self.method = method
        self.uri = uri
        self.cookies = cookies or {}


def make_handler(handler_cls, settings: dict, request: _FakeRequest | None = None, db: Any = None):
    """Создать handler без Tornado __init__.

    `LoginHandler.settings` — read-only property от self.application.settings,
    поэтому подставляем _FakeApp. `self.request`, `self.db` — обычные attributes.
    """
    handler = handler_cls.__new__(handler_cls)
    handler.application = _FakeApp(settings)  # type: ignore[assignment]
    handler.request = request or _FakeRequest()  # type: ignore[assignment]
    handler.db = db if db is not None else object()
    # _headers / _write_buffer init для write_*-helpers
    handler._headers = {}
    handler._write_buffer = []
    handler._status_code = 200
    handler._reason = "OK"
    handler._finished = False
    return handler


# ---------------------------------------------------------------------------
# Domain factories
# ---------------------------------------------------------------------------


def make_otp(
    *,
    otp_id: int = 1,
    phone: str = "+71112223344",
    code_hash: str = "deadbeef",
    attempts: int = 0,
    expires_in_seconds: int = 300,
    used: bool = False,
) -> OtpCode:
    """Сконструировать OtpCode dataclass для возврата из mock'ов.

    `expires_in_seconds` < 0 → просроченный OTP.
    `used=True` → consume() уже сработал (used_at заполнен).
    """
    now = datetime.utcnow()
    return OtpCode(
        id=otp_id,
        phone=phone,
        code_hash=code_hash,
        attempts=attempts,
        expires_at=now + timedelta(seconds=expires_in_seconds),
        used_at=now if used else None,
        created_at=now,
    )


def make_access_token(
    *,
    token_id: int = 1,
    user_id: int = 42,
    token_hash: str = "abc123",
    expires_in_hours: int = 24,
    revoked: bool = False,
) -> AccessToken:
    """Сконструировать AccessToken dataclass для возврата из mock'ов."""
    now = datetime.utcnow()
    return AccessToken(
        id=token_id,
        user_id=user_id,
        token_hash=token_hash,
        expires_at=now + timedelta(hours=expires_in_hours),
        revoked_at=now if revoked else None,
        created_at=now,
        user_agent="pytest",
        ip_address="127.0.0.1",
    )


# ---------------------------------------------------------------------------
# Rate-limit helpers
# ---------------------------------------------------------------------------


def make_rate_limit_repo(check_result: bool = True) -> MagicMock:
    """Мок RateLimitRepository.check_and_record возвращает фиксированный bool.

    Используется в тестах LoginHandler.post(), которые внутри инстанцируют
    RateLimitRepository(self.db). Подменяется через `patch` на имя
    `app.handlers.login.RateLimitRepository`, конструктор возвращает этот мок.
    """
    repo = MagicMock()
    repo.check_and_record = AsyncMock(return_value=check_result)
    return repo
