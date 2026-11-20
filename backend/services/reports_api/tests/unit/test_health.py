"""Unit-тесты для HealthHandler reports_api (shared.auth.HealthHandler).

HealthHandler наследуется от JSONHandler и НЕ требует X-Remote-User.
Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).

Статус/тело/заголовки перехватываем через замыкания вместо реального
Tornado HTTP-стека (он требует HTTPConnection и пр.).
"""
import asyncio
import json
from typing import Any

from shared.auth import HealthHandler
from tests.unit.conftest import make_handler


def _capture(handler) -> dict:
    """Перехват set_status/write/set_header для проверки ответа."""
    captured: dict[str, Any] = {"status": None, "body": None, "headers": {}}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_write(chunk: Any) -> None:
        captured["body"] = chunk

    def fake_set_header(name: str, value: Any) -> None:
        captured["headers"][name] = value

    handler.set_status = fake_set_status
    handler.write = fake_write
    handler.set_header = fake_set_header
    return captured


def test_health_ok(db_mock):
    db_mock.fetch_one.return_value = {"?column?": 1}
    handler = make_handler(HealthHandler, {}, db=db_mock)
    captured = _capture(handler)

    asyncio.run(handler.get())

    assert captured["status"] == 200
    payload = json.loads(captured["body"])
    assert payload["status"] == "ok"
    assert payload["db"] == "ok"


def test_health_degraded_when_db_down(db_mock):
    db_mock.fetch_one.side_effect = RuntimeError("db down")
    handler = make_handler(HealthHandler, {}, db=db_mock)
    captured = _capture(handler)

    asyncio.run(handler.get())

    assert captured["status"] == 503
    payload = json.loads(captured["body"])
    assert payload["status"] == "degraded"
    assert payload["db"] == "unavailable"
