"""Unit-тест: VerifyHandler возвращает name + role в success-ответе.

2FA-путь (login → OTP → verify) должен отдавать те же поля идентичности,
что и login без 2FA (см. login.py: {"message", "name", "role"}). Иначе
фронт после verify не знает, кто вошёл и какая у него роль.
"""
from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

import app.handlers.verify as mod
from app.handlers.verify import VerifyHandler


def _handler(body: dict) -> VerifyHandler:
    h = VerifyHandler.__new__(VerifyHandler)
    app = MagicMock()
    app.settings = {
        "verify_rate_limit": 0,
        "otp_max_attempts": 3,
        "service_name": "auth_api",
        "secure_cookies": False,
        "access_token_ttl_hours": 24,
    }
    h.application = app  # type: ignore[assignment]
    h.request = MagicMock(remote_ip="10.0.0.2", headers={})  # type: ignore[assignment]
    h.db = MagicMock()  # type: ignore[assignment]
    h.get_json_body = MagicMock(return_value=body)  # type: ignore[method-assign]
    h.write_json = MagicMock()  # type: ignore[method-assign]
    h.set_status = MagicMock()  # type: ignore[method-assign]
    h.finish = MagicMock()  # type: ignore[method-assign]
    h._set_access_cookie = MagicMock()  # type: ignore[method-assign]
    h._issue_access_token = AsyncMock(return_value="tok")  # type: ignore[method-assign]
    h._get_user_by_phone = AsyncMock(  # type: ignore[method-assign]
        return_value={"user_id": 7, "name": "Иван Иванов", "role": "worker"},
    )
    return h


def test_verify_success_returns_name_and_role() -> None:
    code = "123456"
    code_hash = hashlib.sha256(("salt" + code).encode()).hexdigest()
    mod.hash_otp = lambda c: code_hash  # type: ignore[attr-defined]
    repo = MagicMock(
        get_active=AsyncMock(return_value=MagicMock(id=1)),
        reserve_attempt=AsyncMock(return_value={"code_hash": code_hash}),
        consume=AsyncMock(return_value=True),
    )
    mod.OtpRepository = MagicMock(return_value=repo)  # type: ignore[attr-defined]

    h = _handler({"phone": "x", "code": code})
    asyncio.run(h.post())

    h.write_json.assert_called_once()
    payload = h.write_json.call_args.args[0]
    assert payload["message"] == "Authentication successful"
    assert payload["name"] == "Иван Иванов"
    assert payload["role"] == "worker"
