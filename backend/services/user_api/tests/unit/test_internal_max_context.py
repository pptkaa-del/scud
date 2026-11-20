"""Unit-тесты InternalMaxContextHandler — GET /internal/user/{id}/max-context.

Проверяет, что ответ несёт phone (нужен auth_api step-up для SMS-primary
доставки OTP), помимо require_2fa/max_user_id.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from unittest.mock import AsyncMock, patch

from app.handlers.internal_max_context import InternalMaxContextHandler
from tests.unit.conftest import _FakeRequest, make_handler

_KEY = b"unit-max-context-hmac-key"


def _sign(path: str, key: bytes, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    body_hex = hashlib.sha256(b"").hexdigest()
    msg = f"{ts}\nGET\n{path}\n{body_hex}".encode()
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return {"X-Internal-Timestamp": ts, "X-Internal-Signature": sig}


def _capture(handler) -> dict:
    captured: dict = {"status": None, "finished": False, "body": None}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_finish(chunk=None) -> None:
        captured["finished"] = True
        if chunk is not None:
            captured["body"] = chunk

    def fake_write_json(data, status_code: int = 200) -> None:
        captured["status"] = status_code
        captured["body"] = data
        handler._status_code = status_code

    handler.set_status = fake_set_status
    handler.finish = fake_finish
    handler.write_json = fake_write_json
    return captured


def _make_ctx_handler(user_id: str = "42", headers: dict | None = None):
    path = f"/internal/user/{user_id}/max-context"
    request = _FakeRequest(method="GET", uri=path, headers=headers or {})
    return make_handler(
        InternalMaxContextHandler,
        settings={"internal_step_up_hmac_key": _KEY},
        request=request,
        db=object(),
    )


def test_happy_path_returns_phone_require_2fa_max_user_id() -> None:
    async def run() -> dict:
        handler = _make_ctx_handler(
            headers=_sign("/internal/user/42/max-context", _KEY),
        )
        cap = _capture(handler)
        with patch(
            "app.handlers.internal_max_context.UserRepository.get_max_context",
            new=AsyncMock(return_value={
                "require_2fa": True,
                "max_user_id": 555000,
                "phone": "+79991234567",
            }),
        ):
            await handler.get("42")
        return cap

    cap = asyncio.run(run())
    assert cap["status"] == 200
    assert cap["body"] == {
        "require_2fa": True,
        "max_user_id": 555000,
        "phone": "+79991234567",
    }


def test_unbound_user_still_returns_phone() -> None:
    """Пользователь включил 2FA, но не привязал Max — phone всё равно есть."""
    async def run() -> dict:
        handler = _make_ctx_handler(
            headers=_sign("/internal/user/42/max-context", _KEY),
        )
        cap = _capture(handler)
        with patch(
            "app.handlers.internal_max_context.UserRepository.get_max_context",
            new=AsyncMock(return_value={
                "require_2fa": True,
                "max_user_id": None,
                "phone": "+79991234567",
            }),
        ):
            await handler.get("42")
        return cap

    cap = asyncio.run(run())
    assert cap["status"] == 200
    assert cap["body"]["max_user_id"] is None
    assert cap["body"]["phone"] == "+79991234567"


def test_unknown_user_returns_404() -> None:
    async def run() -> dict:
        handler = _make_ctx_handler(
            user_id="999",
            headers=_sign("/internal/user/999/max-context", _KEY),
        )
        cap = _capture(handler)
        with patch(
            "app.handlers.internal_max_context.UserRepository.get_max_context",
            new=AsyncMock(return_value=None),
        ):
            await handler.get("999")
        return cap

    cap = asyncio.run(run())
    assert cap["status"] == 404
    assert cap["finished"] is True


def test_bad_signature_returns_401() -> None:
    async def run() -> dict:
        handler = _make_ctx_handler(
            headers=_sign("/internal/user/42/max-context", b"wrong-key"),
        )
        cap = _capture(handler)
        await handler.get("42")
        return cap

    cap = asyncio.run(run())
    assert cap["status"] == 401
