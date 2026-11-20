"""Unit-тесты для UserApiClient."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.httpclient import HTTPError

from app.services.user_api_client import UserApiClient
from shared.http import RetryHTTPClient, UpstreamUnavailableError
from shared.utils import body_sha256, signed_path, verify_request_signature

_READ_KEY = b"unit-test-user-read-key"


def _make_response(body: dict) -> MagicMock:
    resp = MagicMock()
    resp.body = json.dumps(body).encode()
    return resp


def _make_client() -> UserApiClient:
    """UserApiClient с RetryHTTPClient(backoff_base=0) — без задержек в тестах."""
    return UserApiClient(
        base_url="http://test-user:8000",
        http=RetryHTTPClient(backoff_base=0),
    )


class TestCheckCredentials(unittest.TestCase):

    async def _success(self):
        client = _make_client()
        resp = _make_response({"user_id": 42, "require_2fa": False, "name": "x", "role": "worker"})
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            result = await client.check_credentials("+79001234567", "hash")
        self.assertEqual(result, {"user_id": 42, "require_2fa": False, "name": "x", "role": "worker"})

    def test_success_returns_dict(self):
        asyncio.run(self._success())

    async def _401_returns_none(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(401, "Unauthorized"),
        ):
            result = await client.check_credentials("+79001234567", "hash")
        self.assertIsNone(result)

    def test_401_returns_none(self):
        asyncio.run(self._401_returns_none())

    async def _404_returns_none(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(404, "Not Found"),
        ):
            result = await client.check_credentials("+79001234567", "hash")
        self.assertIsNone(result)

    def test_404_returns_none(self):
        asyncio.run(self._404_returns_none())

    async def _500_raises_unavailable(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(500, "Internal Server Error"),
        ) as mock_fetch:
            with self.assertRaises(UpstreamUnavailableError):
                await client.check_credentials("+79001234567", "hash")
            # 5xx ретраится 3 раза → 3 вызова
            self.assertEqual(mock_fetch.call_count, 3)

    def test_500_raises_unavailable_after_retries(self):
        asyncio.run(self._500_raises_unavailable())

    async def _network_raises_unavailable(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=ConnectionRefusedError("user_api unreachable"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.check_credentials("+79001234567", "hash")

    def test_network_error_raises_unavailable(self):
        asyncio.run(self._network_raises_unavailable())

    async def _unexpected_4xx_raises_unavailable(self):
        """400 (validation bug) — не «not found», должен бабблить как 503."""
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(400, "Bad Request"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.check_credentials("+79001234567", "hash")

    def test_400_raises_unavailable(self):
        asyncio.run(self._unexpected_4xx_raises_unavailable())


class TestGetUserByPhone(unittest.TestCase):

    async def _success(self):
        client = _make_client()
        resp = _make_response({"user_id": 42, "name": "Иван Иванов", "role": "worker"})
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            result = await client.get_user_by_phone("+79001234567")
        self.assertEqual(
            result, {"user_id": 42, "name": "Иван Иванов", "role": "worker"},
        )

    def test_success_returns_user_record(self):
        asyncio.run(self._success())

    async def _404(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(404, "Not Found"),
        ):
            result = await client.get_user_by_phone("+79009999999")
        self.assertIsNone(result)

    def test_404_returns_none(self):
        asyncio.run(self._404())

    async def _network(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=OSError("connection reset"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.get_user_by_phone("+79001234567")

    def test_network_raises_unavailable(self):
        asyncio.run(self._network())

    async def _phone_url_encoded(self):
        client = _make_client()
        resp = _make_response({"user_id": 7, "name": "x", "role": "worker"})
        captured: dict = {}

        async def fake_fetch(req, raise_error=True):
            captured["url"] = req.url
            return resp

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=fake_fetch,
        ):
            await client.get_user_by_phone("+7 900 123 45 67")
        # urlencode (form-кодирование): '+' → %2B, пробел → '+'. Это нужно для
        # симметрии подписи: user_api round-trip'ит ту же wire-query через
        # parse_qsl+urlencode в идентичную строку (SEC-10). get_query_argument
        # на стороне user_api декодирует '+' обратно в пробел.
        self.assertIn("phone=%2B7+900+123+45+67", captured["url"])

    def test_phone_url_encoded_in_query(self):
        asyncio.run(self._phone_url_encoded())


class TestGetUser(unittest.TestCase):

    async def _success(self):
        client = _make_client()
        resp = _make_response({"id": 7, "role": "administrator"})
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            return_value=resp,
        ):
            result = await client.get_user(7)
        self.assertEqual(result["role"], "administrator")

    def test_success_returns_dict(self):
        asyncio.run(self._success())

    async def _404(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(404, "Not Found"),
        ):
            result = await client.get_user(9999)
        self.assertIsNone(result)

    def test_404_returns_none(self):
        asyncio.run(self._404())

    async def _500(self):
        client = _make_client()
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(503, "Service Unavailable"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.get_user(7)

    def test_503_raises_unavailable(self):
        asyncio.run(self._500())


class TestUserReadSigning(unittest.TestCase):
    """PII-read запросы (by-phone, by-id) подписываются ключом, когда он задан (SEC-10)."""

    def _client_with_key(self) -> UserApiClient:
        return UserApiClient(
            base_url="http://test-user:8000",
            http=RetryHTTPClient(backoff_base=0),
            user_read_hmac_key=_READ_KEY,
        )

    async def _capture_fetch(self, coro_factory):
        captured: dict = {}

        async def fake_fetch(req, raise_error=True):
            captured["url"] = req.url
            captured["method"] = req.method
            captured["headers"] = dict(req.headers)
            resp = MagicMock()
            resp.body = json.dumps({"user_id": 7, "id": 7, "role": "worker"}).encode()
            return resp

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=fake_fetch,
        ):
            await coro_factory()
        return captured

    def test_by_phone_signs_with_query_bound_path(self):
        """Подпись снята с signed_path(path, qs) — query входит в канон (SEC-10)."""
        client = self._client_with_key()
        cap = asyncio.run(
            self._capture_fetch(lambda: client.get_user_by_phone("+79001234567"))
        )
        ts = cap["headers"]["X-Internal-Timestamp"]
        sig = cap["headers"]["X-Internal-Signature"]
        # wire-query, которую увидит user_api в request.query.
        wire_query = cap["url"].split("?", 1)[1]
        # Подпись валидна над signed_path(path, wire_query) — то, что считает
        # verifier (round-trip симметричен через parse_qsl+urlencode).
        self.assertTrue(verify_request_signature(
            signature=sig, timestamp=ts, method="GET",
            path=signed_path("/internal/user/by-phone", wire_query),
            body_sha256_hex=body_sha256(b""), key=_READ_KEY,
        ))
        # Подпись над path БЕЗ query (старый стиль) теперь НЕ должна совпасть.
        self.assertFalse(verify_request_signature(
            signature=sig, timestamp=ts, method="GET",
            path="/internal/user/by-phone",
            body_sha256_hex=body_sha256(b""), key=_READ_KEY,
        ))
        # Подмена phone= в query после подписи → verify FALSE (anti-tamper).
        tampered_query = "phone=%2B79999999999"
        self.assertFalse(verify_request_signature(
            signature=sig, timestamp=ts, method="GET",
            path=signed_path("/internal/user/by-phone", tampered_query),
            body_sha256_hex=body_sha256(b""), key=_READ_KEY,
        ))

    def test_by_id_signs_concrete_path(self):
        client = self._client_with_key()
        cap = asyncio.run(self._capture_fetch(lambda: client.get_user(42)))
        ts = cap["headers"]["X-Internal-Timestamp"]
        sig = cap["headers"]["X-Internal-Signature"]
        self.assertTrue(verify_request_signature(
            signature=sig, timestamp=ts, method="GET",
            path="/internal/user/42",
            body_sha256_hex=body_sha256(b""), key=_READ_KEY,
        ))
        # Подпись для другого id не валидна.
        self.assertFalse(verify_request_signature(
            signature=sig, timestamp=ts, method="GET",
            path="/internal/user/7",
            body_sha256_hex=body_sha256(b""), key=_READ_KEY,
        ))

    def test_no_key_sends_no_signature_headers(self):
        client = UserApiClient(
            base_url="http://test-user:8000",
            http=RetryHTTPClient(backoff_base=0),
        )
        cap = asyncio.run(
            self._capture_fetch(lambda: client.get_user_by_phone("+79001234567"))
        )
        self.assertNotIn("X-Internal-Signature", cap["headers"])
        self.assertNotIn("X-Internal-Timestamp", cap["headers"])


if __name__ == "__main__":
    unittest.main()
