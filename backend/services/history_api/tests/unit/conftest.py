"""Общие фикстуры для unit-тестов history_api.

Дополнительно: helpers `_FakeApp` / `_FakeRequest` / `make_handler` для
тестов handlers напрямую (без HTTP-стека Tornado). Аналог
`user_api/tests/unit/conftest.py`. См. `test_internal_command_history.py`.
"""

from typing import Any
from unittest.mock import AsyncMock

import pytest


@pytest.fixture
def mock_db():
    """Мок базы данных AsyncPostgres"""
    db = AsyncMock()
    db.execute = AsyncMock()
    db.fetch_one = AsyncMock(return_value=None)
    db.fetch_one_strict = AsyncMock()
    db.fetch_all = AsyncMock(return_value=[])
    return db


# ---------------------------------------------------------------------------
# Tornado stand-ins (см. user_api/tests/unit/conftest.py)
# ---------------------------------------------------------------------------


class _FakeApp:
    """Минимальный заменитель tornado.web.Application.

    `RequestHandler.settings` — read-only property от self.application.settings.
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

    Содержит поля, которые читают handler'ы:
    `remote_ip`, `headers`, `method`, `uri`, `body`, `query_arguments`.
    """

    def __init__(
        self,
        remote_ip: str = "127.0.0.1",
        headers: dict | None = None,
        body: bytes = b"",
        method: str = "GET",
        uri: str = "/",
        cookies: dict | None = None,
        query_arguments: dict | None = None,
    ) -> None:
        self.remote_ip = remote_ip
        self.headers = _FakeHeaders(headers or {})
        self.body = body
        self.method = method
        self.uri = uri
        # Tornado выставляет request.path как компонент пути без query-строки.
        # Handler'ы читают его для HMAC-проверки подписи (SEC-20).
        self.path = uri.split("?", 1)[0]
        self.cookies = cookies or {}
        # tornado.RequestHandler.get_query_argument читает request.query_arguments —
        # dict[str, list[bytes]]. Если не передано, _get_argument вернёт default.
        self.query_arguments = query_arguments or {}
        self.body_arguments: dict = {}
        self.arguments: dict = dict(self.query_arguments)


def make_handler(handler_cls, settings: dict | None = None,
                 request: _FakeRequest | None = None, db: Any = None):
    """Создать handler в обход Tornado __init__.

    Подставляет _FakeApp + _FakeRequest, инициализирует
    _headers/_write_buffer/_status_code чтобы write_json/set_status/finish
    работали без полного HTTP-стека.
    """
    handler = handler_cls.__new__(handler_cls)
    handler.application = _FakeApp(settings or {})  # type: ignore[assignment]
    handler.request = request or _FakeRequest()  # type: ignore[assignment]
    handler.db = db if db is not None else object()
    handler._headers = {}
    handler._write_buffer = []
    handler._status_code = 200
    handler._reason = "OK"
    handler._finished = False
    return handler
