"""Общие фикстуры и моки для unit-тестов Reports API.

ВАЖНО: AsyncHTTPTestCase от Tornado не совместим с механизмом фикстур pytest.
Для таких тестов моки создаются напрямую в методе get_app() классов тестов.

Дополнительно: helpers `_FakeApp` / `_FakeRequest` / `make_handler` для
тестов handlers напрямую (без HTTP-стека Tornado).
"""
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from tornado.web import Application

from app.main import make_app

# ---------------------------------------------------------------------------
# Tornado stand-ins (см. auth_api/tests/unit/conftest.py)
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

    def get(self, key: str, default: Any = None) -> Any:
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
        self.cookies = cookies or {}
        self.query_arguments = query_arguments or {}
        self.body_arguments: dict = {}
        self.arguments: dict = dict(self.query_arguments)


def make_handler(handler_cls, settings: dict | None = None,
                 request: _FakeRequest | None = None, db: Any = None):
    """Создать handler в обход Tornado __init__.

    Подставляет _FakeApp + _FakeRequest, инициализирует
    _headers/_write_buffer/_status_code чтобы write/set_status/finish
    работали без полного HTTP-стека.
    """
    handler = handler_cls.__new__(handler_cls)
    handler.application = _FakeApp(settings or {})
    handler.request = request or _FakeRequest()
    handler.db = db if db is not None else object()
    handler._headers = {}
    handler._write_buffer = []
    handler._status_code = 200
    handler._reason = "OK"
    handler._finished = False
    return handler


@pytest.fixture
def db_mock():
    """Создаёт мок базы данных с основными методами."""
    mock = MagicMock()
    mock.fetch_one = AsyncMock()
    mock.fetch_one_strict = AsyncMock()
    mock.fetch_all = AsyncMock()
    mock.execute = AsyncMock()
    return mock


@pytest.fixture
def test_app(db_mock) -> Application:
    """Создаёт тестовое приложение Tornado с моком БД."""
    return make_app(db_mock, registry={})
