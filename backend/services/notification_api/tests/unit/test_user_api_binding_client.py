"""Unit-тесты UserApiBindingClient — HMAC-подписанный вызов user_api.

`UserApiBindingClient.bind_max_user` отправляет POST /internal/user/by-invite,
подписывая запрос HMAC-SHA256 над `<ts>\n<METHOD>\n<path>\n<sha256(body)>`.
Проверяем:

- При заданном `hmac_key` запрос несёт корректные X-Internal-Timestamp /
  X-Internal-Signature, и подпись верифицируется тем же ключом (round-trip).
- Без ключа подпись не добавляется.
- Маппинг кодов upstream: 200→BOUND(user_id), 404→TOKEN_INVALID,
  409→CONFLICT, 5xx / сеть → UpstreamUnavailableError.
- Деградации 200-ответа (не-JSON / нет user_id) → UpstreamUnavailableError.

Мокаем сам `RetryHTTPClient.fetch` (а не низкоуровневый AsyncHTTPClient),
чтобы изолировать клиента от ретраев и проверить переданные заголовки.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from tornado.httpclient import HTTPError

from app.services.user_api_binding_client import (
    BindMaxResult,
    UserApiBindingClient,
)
from shared.http import RetryHTTPClient, UpstreamUnavailableError
from shared.utils import (
    body_sha256,
    compute_request_signature,
    verify_request_signature,
)

_HMAC_KEY = b"binding-unit-test-key"
_BASE = "http://user_api:8000"
_BIND_PATH = "/internal/user/by-invite"


def _make_resp(body: dict | str, code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.code = code
    resp.body = (json.dumps(body) if not isinstance(body, str) else body).encode()
    return resp


def _make_client(hmac_key: bytes | None = _HMAC_KEY) -> tuple[UserApiBindingClient, MagicMock]:
    http = MagicMock(spec=RetryHTTPClient)
    http.fetch = AsyncMock()
    client = UserApiBindingClient(_BASE, http=http, hmac_key=hmac_key)
    return client, http


# ── HMAC-подпись ─────────────────────────────────────────────────────────────


def test_request_is_hmac_signed_and_verifiable() -> None:
    """Запрос подписан, и подпись проходит verify тем же ключом (round-trip)."""
    client, http = _make_client()
    http.fetch.return_value = _make_resp({"user_id": 42}, code=200)

    asyncio.run(client.bind_max_user(invite_token="tok-abc", max_user_id=99))

    http.fetch.assert_awaited_once()
    _, kwargs = http.fetch.call_args
    headers = kwargs["headers"]
    body = kwargs["body"]

    ts = headers["X-Internal-Timestamp"]
    sig = headers["X-Internal-Signature"]

    # Подпись соответствует ровно тому body, что ушёл в fetch.
    expected = compute_request_signature(
        timestamp=ts, method="POST", path=_BIND_PATH,
        body_sha256_hex=body_sha256(body), key=_HMAC_KEY,
    )
    assert sig == expected
    assert verify_request_signature(
        signature=sig, timestamp=ts, method="POST", path=_BIND_PATH,
        body_sha256_hex=body_sha256(body), key=_HMAC_KEY,
    )
    # Body несёт ровно переданные поля.
    assert json.loads(body) == {"invite_token": "tok-abc", "max_user_id": 99}


def test_no_signature_headers_when_key_absent() -> None:
    """Без hmac_key подпись/таймстамп не добавляются."""
    client, http = _make_client(hmac_key=None)
    http.fetch.return_value = _make_resp({"user_id": 7}, code=200)

    asyncio.run(client.bind_max_user(invite_token="tok", max_user_id=1))

    _, kwargs = http.fetch.call_args
    headers = kwargs["headers"]
    assert "X-Internal-Signature" not in headers
    assert "X-Internal-Timestamp" not in headers


# ── Маппинг кодов upstream ───────────────────────────────────────────────────


def test_200_returns_bound_with_user_id() -> None:
    client, http = _make_client()
    http.fetch.return_value = _make_resp({"user_id": 12345}, code=200)

    resp = asyncio.run(
        client.bind_max_user(invite_token="t", max_user_id=1)
    )
    assert resp.result is BindMaxResult.BOUND
    assert resp.user_id == 12345


def test_404_returns_token_invalid() -> None:
    client, http = _make_client()
    http.fetch.side_effect = HTTPError(404, "Not Found")

    resp = asyncio.run(client.bind_max_user(invite_token="t", max_user_id=1))
    assert resp.result is BindMaxResult.TOKEN_INVALID
    assert resp.user_id is None


def test_409_returns_conflict() -> None:
    client, http = _make_client()
    http.fetch.side_effect = HTTPError(409, "Conflict")

    resp = asyncio.run(client.bind_max_user(invite_token="t", max_user_id=1))
    assert resp.result is BindMaxResult.CONFLICT


def test_500_raises_upstream_unavailable() -> None:
    client, http = _make_client()
    http.fetch.side_effect = HTTPError(500, "boom")

    async def run() -> None:
        with pytest.raises(UpstreamUnavailableError) as ctx:
            await client.bind_max_user(invite_token="t", max_user_id=1)
        assert ctx.value.status == 500

    asyncio.run(run())


def test_network_error_raises_upstream_unavailable() -> None:
    client, http = _make_client()
    http.fetch.side_effect = ConnectionRefusedError("no route")

    async def run() -> None:
        with pytest.raises(UpstreamUnavailableError):
            await client.bind_max_user(invite_token="t", max_user_id=1)

    asyncio.run(run())


def test_200_missing_user_id_raises() -> None:
    """200 без числового user_id → UpstreamUnavailableError (нельзя верить
    деградировавшему ответу)."""
    client, http = _make_client()
    http.fetch.return_value = _make_resp({"status": "ok"}, code=200)

    async def run() -> None:
        with pytest.raises(UpstreamUnavailableError):
            await client.bind_max_user(invite_token="t", max_user_id=1)

    asyncio.run(run())


def test_200_non_json_raises() -> None:
    client, http = _make_client()
    http.fetch.return_value = _make_resp("<<not json>>", code=200)

    async def run() -> None:
        with pytest.raises(UpstreamUnavailableError):
            await client.bind_max_user(invite_token="t", max_user_id=1)

    asyncio.run(run())
