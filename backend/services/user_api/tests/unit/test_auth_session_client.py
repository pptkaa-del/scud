"""Unit-тесты AuthApiSessionClient — клиент user_api → auth_api (SEC-14)."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.httpclient import HTTPError

from app.services.auth_session_client import AuthApiSessionClient
from shared.http import RetryHTTPClient


def _make_resp(body: bytes = b'{"revoked": 2}', code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.code = code
    resp.body = body
    return resp


def _make_client(hmac_key: bytes | None = None) -> AuthApiSessionClient:
    return AuthApiSessionClient(
        base_url="http://test-auth:8003",
        http=RetryHTTPClient(backoff_base=0),
        hmac_key=hmac_key,
    )


class TestRevokeUserSessions(unittest.TestCase):

    def test_returns_revoked_count(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(body=b'{"revoked": 5}'),
            ):
                result = await client.revoke_user_sessions(user_id=42)
            self.assertEqual(result, 5)
        asyncio.run(run())

    def test_posts_user_id_in_body(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(),
            ) as mock_fetch:
                await client.revoke_user_sessions(user_id=77)
            req = mock_fetch.call_args.args[0]
            self.assertEqual(req.method, "POST")
            self.assertTrue(req.url.endswith("/internal/api/v1/sessions/revoke"))
            self.assertEqual(json.loads(req.body), {"user_id": 77})
        asyncio.run(run())

    def test_signature_attached_when_hmac_key_set(self):
        async def run():
            client = _make_client(hmac_key=b"key")
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(),
            ) as mock_fetch:
                await client.revoke_user_sessions(user_id=1)
            req = mock_fetch.call_args.args[0]
            self.assertIn("X-Internal-Timestamp", req.headers)
            self.assertIn("X-Internal-Signature", req.headers)
        asyncio.run(run())

    def test_no_signature_when_hmac_key_none(self):
        async def run():
            client = _make_client(hmac_key=None)
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(),
            ) as mock_fetch:
                await client.revoke_user_sessions(user_id=1)
            req = mock_fetch.call_args.args[0]
            self.assertNotIn("X-Internal-Timestamp", req.headers)
            self.assertNotIn("X-Internal-Signature", req.headers)
        asyncio.run(run())

    def test_propagates_http_error(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(503, "boom"),
            ), self.assertRaises(HTTPError):
                await client.revoke_user_sessions(user_id=1)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
