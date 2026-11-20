import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from tornado.httpclient import HTTPError

from app.services.user_client import UserApiClient
from shared.http.errors import UpstreamUnavailableError
from shared.utils import body_sha256, signed_path, verify_request_signature

_BASE = "http://user_api:8000"
_READ_KEY = b"unit-test-user-read-key"


def _resp(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.body = json.dumps(payload).encode()
    return resp


def test_fetch_names_parses_mapping():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp({"names": {"7": "Иванов", "8": "Петров"}}))
    client = UserApiClient(_BASE, http=http)

    names = asyncio.run(client.fetch_names([7, 8]))

    assert names == {7: "Иванов", 8: "Петров"}
    http.fetch.assert_awaited_once()
    url = http.fetch.await_args.args[0]
    assert url.startswith(f"{_BASE}/internal/users/names?")
    assert "ids=7%2C8" in url  # urlencoded "7,8"


def test_fetch_names_empty_skips_http():
    http = MagicMock()
    http.fetch = AsyncMock()
    client = UserApiClient(_BASE, http=http)

    assert asyncio.run(client.fetch_names([])) == {}
    http.fetch.assert_not_awaited()


def test_fetch_names_missing_names_key():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp({}))
    client = UserApiClient(_BASE, http=http)

    assert asyncio.run(client.fetch_names([7])) == {}


def test_fetch_names_http_error_raises_upstream():
    http = MagicMock()
    http.fetch = AsyncMock(side_effect=HTTPError(503, "boom"))
    client = UserApiClient(_BASE, http=http)

    with pytest.raises(UpstreamUnavailableError) as exc_info:
        asyncio.run(client.fetch_names([7]))

    assert exc_info.value.upstream == "user_api"
    assert exc_info.value.status == 503


def test_fetch_names_network_error_raises_upstream():
    http = MagicMock()
    http.fetch = AsyncMock(side_effect=ConnectionError("refused"))
    client = UserApiClient(_BASE, http=http)

    with pytest.raises(UpstreamUnavailableError) as exc_info:
        asyncio.run(client.fetch_names([7]))

    assert exc_info.value.upstream == "user_api"
    assert exc_info.value.status is None


def test_fetch_names_signs_query_bound_path_when_key_set():
    """Подпись снята с signed_path(path, qs) — ids= входит в канон (SEC-10)."""
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp({"names": {}}))
    client = UserApiClient(_BASE, http=http, hmac_key=_READ_KEY)

    asyncio.run(client.fetch_names([7, 8]))

    headers = http.fetch.await_args.kwargs["headers"]
    ts = headers["X-Internal-Timestamp"]
    sig = headers["X-Internal-Signature"]
    url = http.fetch.await_args.args[0]
    wire_query = url.split("?", 1)[1]
    # Подпись валидна над signed_path(path, wire_query) — то, что считает verifier.
    assert verify_request_signature(
        signature=sig, timestamp=ts, method="GET",
        path=signed_path("/internal/users/names", wire_query),
        body_sha256_hex=body_sha256(b""), key=_READ_KEY,
    )
    # Подпись над path БЕЗ query (старый стиль) теперь НЕ совпадает.
    assert not verify_request_signature(
        signature=sig, timestamp=ts, method="GET",
        path="/internal/users/names",
        body_sha256_hex=body_sha256(b""), key=_READ_KEY,
    )
    # Подмена ids= в query после подписи → verify False (anti-tamper).
    assert not verify_request_signature(
        signature=sig, timestamp=ts, method="GET",
        path=signed_path("/internal/users/names", "ids=1%2C2%2C3"),
        body_sha256_hex=body_sha256(b""), key=_READ_KEY,
    )


def test_fetch_names_no_signature_headers_when_key_none():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp({"names": {}}))
    client = UserApiClient(_BASE, http=http)  # ключ не задан

    asyncio.run(client.fetch_names([7]))

    headers = http.fetch.await_args.kwargs["headers"]
    assert "X-Internal-Signature" not in headers
    assert "X-Internal-Timestamp" not in headers
