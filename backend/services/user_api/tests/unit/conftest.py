"""Общие фикстуры и моки для unit-тестов

ВАЖНО: AsyncHTTPTestCase от Tornado не совместим с механизмом фикстур pytest.
Для таких тестов моки создаются напрямую в методе get_app() классов тестов.

Фикстуры ниже оставлены для справки и могут использоваться в обычных pytest-тестах
(не использующих AsyncHTTPTestCase).

Дополнительно: helpers `_FakeApp` / `_FakeRequest` / `make_handler` для
тестов handlers напрямую (без HTTP-стека Tornado). См. test_internal_auth.py.
"""
from datetime import datetime
from typing import Any, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from tornado.web import Application

from app.main import make_app
from app.user_data.db.models.user import UserState

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
        method: str = "POST",
        uri: str = "/",
        cookies: dict | None = None,
        query_arguments: dict | None = None,
    ) -> None:
        self.remote_ip = remote_ip
        self.headers = _FakeHeaders(headers or {})
        self.body = body
        self.method = method
        self.uri = uri
        # Tornado выставляет request.path как компонент пути без query-строки,
        # а request.query — как raw query-строку после '?' (пустая, если нет).
        # Handler'ы читают оба для HMAC-проверки подписи: path для SEC-20,
        # query сворачивается в signed_path для SEC-10.
        path_and_query = uri.split("?", 1)
        self.path = path_and_query[0]
        self.query = path_and_query[1] if len(path_and_query) > 1 else ""
        self.cookies = cookies or {}
        # tornado.RequestHandler.get_query_argument читает request.query_arguments —
        # dict[str, list[bytes]]. Если не передано, _get_argument вернёт default.
        self.query_arguments = query_arguments or {}
        self.body_arguments: dict = {}
        self.arguments: dict = dict(self.query_arguments)


def make_handler(handler_cls, settings: dict | None = None,
                 request: _FakeRequest | None = None, db: Any = None):
    """Создать handler в обход Tornado __init__.

    Аналог auth_api/tests/unit/conftest.py:_make_handler. Подставляет _FakeApp
    + _FakeRequest, инициализирует _headers/_write_buffer/_status_code чтобы
    write_json/set_status/finish работали без полного HTTP-стека.
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


@pytest.fixture
def db_mock():
    """Создает мок базы данных с основными методами

    Примечание: Не используется в AsyncHTTPTestCase тестах.
    Для AsyncHTTPTestCase создавайте моки напрямую в get_app():

        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
    """
    mock = MagicMock()
    mock.fetch_one = AsyncMock()
    mock.fetch_one_strict = AsyncMock()
    mock.fetch_all = AsyncMock()
    mock.execute = AsyncMock()
    return mock


@pytest.fixture
def test_app(db_mock) -> Application:
    """Создает тестовое приложение Tornado с моком БД

    Примечание: Не используется в AsyncHTTPTestCase тестах.
    """
    return make_app(db_mock)


@pytest.fixture
def auth_user_row():
    """Возвращает данные аутентифицированного пользователя для тестов

    Примечание: Не используется в AsyncHTTPTestCase тестах.
    """
    return {
        "id": 1,
        "phone": "+79123456789",
        "full_name": "Admin",
        "contractor_id": None,
        "contractor_name": None,
        "created_at": datetime.now(),
        "created_by": None,
        "updated_at": datetime.now(),
        "updated_by": None,
        "state": UserState.ACTIVE.value,
        "role": "administrator"
    }
