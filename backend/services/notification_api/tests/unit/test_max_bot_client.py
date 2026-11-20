"""Unit-тесты MaxBotClient — REST-обёртки Max Bot API."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.httpclient import HTTPError

from app.services.max_bot_client import (
    MaxBotClient,
    MaxRecipientUnreachableError,
    MaxUpdates,
)
from shared.http import RetryHTTPClient, UpstreamUnavailableError


def _make_resp(body: dict | list | str, code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.code = code
    resp.body = (json.dumps(body) if not isinstance(body, str) else body).encode()
    return resp


def _make_client() -> MaxBotClient:
    return MaxBotClient(
        http=RetryHTTPClient(backoff_base=0),
        base_url="http://test-max-api",
        token="test-token",
    )


class TestSendText(unittest.TestCase):

    def test_empty_token_rejected(self):
        with self.assertRaises(ValueError):
            MaxBotClient(http=RetryHTTPClient(backoff_base=0), base_url="x", token="")

    def test_empty_text_rejected(self):
        async def run():
            with self.assertRaises(ValueError):
                await _make_client().send_text(42, "")
        asyncio.run(run())

    def test_overlong_text_rejected(self):
        async def run():
            with self.assertRaises(ValueError):
                await _make_client().send_text(42, "x" * 4001)
        asyncio.run(run())

    def test_success(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp({"id": "msg-1"}, code=200),
            ) as mock_fetch:
                await client.send_text(42, "Код: 123456")
            args, kwargs = mock_fetch.call_args
            req = args[0]
            self.assertEqual(req.method, "POST")
            self.assertIn("user_id=42", req.url)
            self.assertEqual(req.headers["Authorization"], "test-token")
            payload = json.loads(req.body)
            self.assertEqual(payload["text"], "Код: 123456")
        asyncio.run(run())

    def test_400_raises_recipient_unreachable(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(400, "Bad Request"),
            ):
                with self.assertRaises(MaxRecipientUnreachableError) as ctx:
                    await client.send_text(42, "x")
                self.assertEqual(ctx.exception.max_user_id, 42)
                self.assertEqual(ctx.exception.status, 400)
        asyncio.run(run())

    def test_404_raises_recipient_unreachable(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(404, "Not Found"),
            ), self.assertRaises(MaxRecipientUnreachableError):
                await client.send_text(42, "x")
        asyncio.run(run())

    def test_401_raises_upstream_unavailable(self):
        # 401 — это auth-проблема бота, не получателя. Падать через
        # MaxRecipientUnreachableError нельзя — caller начнёт ошибочно
        # помечать outbox как failed. Это UpstreamUnavailable.
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(401, "Unauthorized"),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.send_text(42, "x")
        asyncio.run(run())

    def test_500_not_retried_raises(self):
        # send_text — POST к Max без дедупликации (event_uid нет). 5xx —
        # неоднозначный сбой: upstream мог уже доставить сообщение. Повтор
        # продублировал бы OTP/уведомление, поэтому POST НЕ ретраится
        # (COR-11) — одна попытка, затем UpstreamUnavailableError.
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(500, "boom"),
            ) as mock_fetch:
                with self.assertRaises(UpstreamUnavailableError):
                    await client.send_text(42, "x")
                self.assertEqual(mock_fetch.call_count, 1)
        asyncio.run(run())

    def test_timeout_not_retried_raises(self):
        # Таймаут POST — ровно неоднозначный случай (сообщение могло уйти).
        # Не ретраим (COR-11): одна попытка, UpstreamUnavailableError.
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=TimeoutError("timed out"),
            ) as mock_fetch:
                with self.assertRaises(UpstreamUnavailableError):
                    await client.send_text(42, "x")
                self.assertEqual(mock_fetch.call_count, 1)
        asyncio.run(run())

    def test_max_client_satisfies_transport_and_accepts_str_address(self):
        from app.services.max_bot_client import MaxRecipientUnreachableError
        from app.services.transport import RecipientUnreachableError
        from shared.enums import NotificationChannel

        async def run():
            assert issubclass(MaxRecipientUnreachableError, RecipientUnreachableError)

            http = MagicMock()
            resp = MagicMock()
            resp.code = 200
            http.fetch = AsyncMock(return_value=resp)
            c = MaxBotClient(http=http, base_url="http://max", token="t")
            assert c.channel == NotificationChannel.MAX
            await c.send_text("12345", "hi")       # str address accepted
            assert "user_id=12345" in http.fetch.call_args.args[0]
        asyncio.run(run())


class TestPollUpdates(unittest.TestCase):

    def test_empty(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp({"updates": [], "marker": None}),
            ):
                result = await client.poll_updates(marker=None, long_poll_timeout_s=1)
            self.assertIsInstance(result, MaxUpdates)
            self.assertEqual(result.updates, [])
            self.assertIsNone(result.marker)
        asyncio.run(run())

    def test_parses_bot_started_with_start_payload(self):
        async def run():
            client = _make_client()
            # Реальная схема Max Bot API: payload — строка start-параметра,
            # числовой id автора во вложенном объекте user, marker — int.
            payload = {
                "updates": [
                    {
                        "update_type": "bot_started",
                        "timestamp": 1700000000,
                        "chat_id": 5,
                        "user": {"user_id": 99, "first_name": "Ivan"},
                        "payload": "invite-abc",
                        "user_locale": "ru",
                    },
                ],
                "marker": 3811,
            }
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(payload),
            ):
                result = await client.poll_updates(marker=3810, long_poll_timeout_s=1)
            self.assertEqual(result.marker, 3811)
            self.assertEqual(len(result.updates), 1)
            upd = result.updates[0]
            self.assertEqual(upd.update_type, "bot_started")
            self.assertEqual(upd.max_user_id, 99)
            self.assertEqual(upd.start_payload, "invite-abc")
        asyncio.run(run())

    def test_skips_unparseable_update_keeps_good_ones(self):
        # Один кривой апдейт (payload-объект вместо строки) не должен ронять
        # весь батч: его пропускаем, валидный bot_started парсим.
        async def run():
            client = _make_client()
            payload = {
                "updates": [
                    {"update_type": "weird", "timestamp": 1, "payload": {"nested": 1}},
                    {
                        "update_type": "bot_started",
                        "timestamp": 2,
                        "user": {"user_id": 7},
                        "payload": "tok",
                    },
                ],
                "marker": 42,
            }
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp(payload),
            ):
                result = await client.poll_updates(marker=None, long_poll_timeout_s=1)
            self.assertEqual(result.marker, 42)
            self.assertEqual(len(result.updates), 1)
            self.assertEqual(result.updates[0].start_payload, "tok")
            self.assertEqual(result.updates[0].max_user_id, 7)
        asyncio.run(run())

    def test_marker_passed_in_query(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp({"updates": [], "marker": None}),
            ) as mock_fetch:
                await client.poll_updates(marker=12345, long_poll_timeout_s=5)
            req = mock_fetch.call_args.args[0]
            self.assertIn("marker=12345", req.url)
            self.assertIn("timeout=5", req.url)
        asyncio.run(run())

    def test_non_dict_body_raises(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_resp([1, 2, 3]),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.poll_updates(marker=None, long_poll_timeout_s=1)
        asyncio.run(run())

    def test_500_raises(self):
        async def run():
            client = _make_client()
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(500, "boom"),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.poll_updates(marker=None, long_poll_timeout_s=1)
        asyncio.run(run())


class TestSeparateSendClient(unittest.TestCase):

    def _clients(self):
        poll_http, send_http = MagicMock(), MagicMock()
        ok = _make_resp({"updates": [], "marker": None})
        poll_http.fetch = AsyncMock(return_value=ok)
        send_http.fetch = AsyncMock(return_value=_make_resp({}))
        client = MaxBotClient(
            http=poll_http, send_http=send_http, base_url="http://max", token="t",
        )
        return client, poll_http, send_http

    def test_send_text_uses_send_client(self):
        async def run():
            client, poll_http, send_http = self._clients()
            await client.send_text("1", "hi")
            send_http.fetch.assert_awaited_once()
            poll_http.fetch.assert_not_called()
        asyncio.run(run())

    def test_poll_updates_uses_polling_client(self):
        async def run():
            client, poll_http, send_http = self._clients()
            await client.poll_updates(marker=None, long_poll_timeout_s=1)
            poll_http.fetch.assert_awaited_once()
            send_http.fetch.assert_not_called()
        asyncio.run(run())

    def test_send_client_defaults_to_http(self):
        async def run():
            http = MagicMock()
            http.fetch = AsyncMock(return_value=_make_resp({}))
            client = MaxBotClient(http=http, base_url="http://max", token="t")
            await client.send_text("1", "hi")
            http.fetch.assert_awaited_once()
        asyncio.run(run())

    def test_send_is_post_and_ambiguous_failure_is_not_retried(self):
        async def run():
            client = MaxBotClient(
                http=RetryHTTPClient(backoff_base=0), base_url="http://max", token="t",
            )
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                side_effect=HTTPError(599, "timeout"),
            ) as fetch, self.assertRaises(UpstreamUnavailableError):
                await client.send_text("1", "code")
            assert fetch.await_count == 1
            assert fetch.await_args.args[0].method == "POST"
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
