"""Unit tests для SessionRevokeHandler (SEC-14).

Pytest-asyncio в Astra отсутствует — все async-проверки в asyncio.run.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import ClassVar
from unittest.mock import AsyncMock, MagicMock, patch

from app.handlers.session_revoke import (
    SESSION_REVOKE_METHOD,
    SESSION_REVOKE_PATH,
    SessionRevokeHandler,
)
from shared.utils import body_sha256, compute_request_signature

_KEY = b"test-session-revoke-hmac-key-32-bytes-pad"

_OTHER_PATH = "/internal/api/v1/step-up/issue"


def _sign(body: bytes, path: str = SESSION_REVOKE_PATH) -> tuple[str, str]:
    ts = str(int(time.time()))
    sig = compute_request_signature(
        ts, SESSION_REVOKE_METHOD, path, body_sha256(body), _KEY,
    )
    return ts, sig


def test_signed_revokes_and_returns_count() -> None:
    async def _run() -> None:
        body = json.dumps({"user_id": 42}).encode()
        ts, sig = _sign(body)
        repo = AsyncMock()
        repo.revoke_by_user_id = AsyncMock(return_value=3)

        handler = _make_handler(
            body=body, ts=ts, sig=sig,
            settings={"internal_session_revoke_hmac_key": _KEY},
        )
        with patch(
            "app.handlers.session_revoke.TokenRepository",
            return_value=repo,
        ):
            await handler.post()

        repo.revoke_by_user_id.assert_awaited_once_with(42)
        payload = json.loads(handler._written)
        assert payload["revoked"] == 3

    asyncio.run(_run())


def test_unsigned_with_key_set_returns_401() -> None:
    async def _run() -> None:
        body = json.dumps({"user_id": 42}).encode()
        repo = AsyncMock()
        handler = _make_handler(
            body=body, ts="", sig="",
            settings={"internal_session_revoke_hmac_key": _KEY},
        )
        with patch(
            "app.handlers.session_revoke.TokenRepository",
            return_value=repo,
        ):
            await handler.post()
        handler.write_unauthorized.assert_called_once()
        repo.revoke_by_user_id.assert_not_awaited()

    asyncio.run(_run())


def test_signature_for_other_path_returns_401() -> None:
    """SEC-20: подпись, снятая для другого пути, не принимается."""
    async def _run() -> None:
        body = json.dumps({"user_id": 42}).encode()
        ts, sig = _sign(body, _OTHER_PATH)
        repo = AsyncMock()
        handler = _make_handler(
            body=body, ts=ts, sig=sig,
            settings={"internal_session_revoke_hmac_key": _KEY},
        )
        with patch(
            "app.handlers.session_revoke.TokenRepository",
            return_value=repo,
        ):
            await handler.post()
        handler.write_unauthorized.assert_called_once()
        repo.revoke_by_user_id.assert_not_awaited()

    asyncio.run(_run())


def test_no_key_fail_open_revokes() -> None:
    """Ключ не задан (dev/CI) — подпись не требуется, revoke проходит."""
    async def _run() -> None:
        body = json.dumps({"user_id": 7}).encode()
        repo = AsyncMock()
        repo.revoke_by_user_id = AsyncMock(return_value=0)
        handler = _make_handler(body=body, ts="", sig="", settings={})
        with patch(
            "app.handlers.session_revoke.TokenRepository",
            return_value=repo,
        ):
            await handler.post()
        repo.revoke_by_user_id.assert_awaited_once_with(7)
        payload = json.loads(handler._written)
        assert payload["revoked"] == 0

    asyncio.run(_run())


def test_bad_body_returns_400() -> None:
    async def _run() -> None:
        body = json.dumps({"user_id": "not-an-int"}).encode()
        ts, sig = _sign(body)
        repo = AsyncMock()
        handler = _make_handler(
            body=body, ts=ts, sig=sig,
            settings={"internal_session_revoke_hmac_key": _KEY},
        )
        with patch(
            "app.handlers.session_revoke.TokenRepository",
            return_value=repo,
        ):
            await handler.post()
        handler.write_validation_error.assert_called_once()
        repo.revoke_by_user_id.assert_not_awaited()

    asyncio.run(_run())


def test_bool_user_id_rejected_400() -> None:
    async def _run() -> None:
        body = json.dumps({"user_id": True}).encode()
        ts, sig = _sign(body)
        repo = AsyncMock()
        handler = _make_handler(
            body=body, ts=ts, sig=sig,
            settings={"internal_session_revoke_hmac_key": _KEY},
        )
        with patch(
            "app.handlers.session_revoke.TokenRepository",
            return_value=repo,
        ):
            await handler.post()
        handler.write_validation_error.assert_called_once()
        repo.revoke_by_user_id.assert_not_awaited()

    asyncio.run(_run())


# --- helpers ---

def _make_handler(*, body, ts, sig, settings, path=SESSION_REVOKE_PATH):
    h = SessionRevokeHandler.__new__(SessionRevokeHandler)
    h._written = ""

    class _Req:
        headers: ClassVar[dict[str, str]] = {
            "X-Internal-Timestamp": ts,
            "X-Internal-Signature": sig,
        }
        remote_ip = "127.0.0.1"
        method = SESSION_REVOKE_METHOD
    _Req.body = body
    _Req.path = path
    h.request = _Req()
    h.application = MagicMock(settings=settings)
    h.set_status = MagicMock()
    h.finish = MagicMock()
    h.write_unauthorized = MagicMock()
    h.write_validation_error = MagicMock()

    def _write(payload, status_code=200):
        h._written = json.dumps(payload) if isinstance(payload, dict) else payload
        h.set_status(status_code)
    h.write_json = MagicMock(side_effect=_write)

    h.db = MagicMock()
    return h
