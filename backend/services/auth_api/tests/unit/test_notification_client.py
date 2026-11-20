"""Unit-тесты NotificationClient — клиент auth_api → notification_api."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.httpclient import HTTPError

from app.services.notification_client import NotificationClient, OtpDeliveryStatus
from shared.http import RetryHTTPClient, UpstreamUnavailableError


def _make_resp(body: bytes = b"", code: int = 204) -> MagicMock:
    resp = MagicMock()
    resp.code = code
    resp.body = body
    return resp


def _make_client(hmac_key: bytes | None = None) -> NotificationClient:
    return NotificationClient(
        base_url="http://test-notif:8005",
        http=RetryHTTPClient(backoff_base=0),
        hmac_key=hmac_key,
    )


class TestRequestOtp(unittest.TestCase):

    def test_204_returns_sent(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(code=204),
            ):
                result = await client.request_otp(
                    user_id=1, max_user_id=99, code="123456", ttl_seconds=300,
                )
            self.assertEqual(result, OtpDeliveryStatus.SENT)
        asyncio.run(run())

    def test_200_queued_returns_queued(self):
        async def run():
            client = _make_client()
            body = json.dumps({"status": "queued_until_binding"}).encode()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(body=body, code=200),
            ):
                result = await client.request_otp(
                    user_id=1, max_user_id=None, code="123456", ttl_seconds=300,
                )
            self.assertEqual(result, OtpDeliveryStatus.QUEUED_UNTIL_BINDING)
        asyncio.run(run())

    def test_200_unknown_status_raises(self):
        async def run():
            client = _make_client()
            body = json.dumps({"status": "something-else"}).encode()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(body=body, code=200),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.request_otp(
                    user_id=1, max_user_id=None, code="x", ttl_seconds=300,
                )
        asyncio.run(run())

    def test_503_raises_upstream_unavailable(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(503, "boom"),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.request_otp(
                    user_id=1, max_user_id=99, code="123456", ttl_seconds=300,
                )
        asyncio.run(run())

    def test_401_raises_upstream_unavailable(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(401, "bad signature"),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.request_otp(
                    user_id=1, max_user_id=99, code="x", ttl_seconds=300,
                )
        asyncio.run(run())

    def test_signature_attached_when_hmac_key_set(self):
        async def run():
            client = _make_client(hmac_key=b"key")
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(code=204),
            ) as mock_fetch:
                await client.request_otp(
                    user_id=1, max_user_id=99, code="x", ttl_seconds=300,
                )
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
                return_value=_make_resp(code=204),
            ) as mock_fetch:
                await client.request_otp(
                    user_id=1, max_user_id=99, code="x", ttl_seconds=300,
                )
            req = mock_fetch.call_args.args[0]
            self.assertNotIn("X-Internal-Timestamp", req.headers)
            self.assertNotIn("X-Internal-Signature", req.headers)
        asyncio.run(run())

    def test_request_otp_includes_address_in_body(self):
        async def run():
            http = MagicMock()
            resp = MagicMock()
            resp.code = 204
            http.fetch = AsyncMock(return_value=resp)
            client = NotificationClient(
                base_url="http://test-notif:8005",
                http=http,
                hmac_key=None,
            )
            result = await client.request_otp(
                user_id=1, max_user_id=None, code="123456",
                ttl_seconds=300, phone="+79001234567"
            )
            self.assertEqual(result, OtpDeliveryStatus.SENT)
            body_bytes = http.fetch.call_args.kwargs["body"]
            body = json.loads(body_bytes)
            self.assertEqual(body["address"], "+79001234567")
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
