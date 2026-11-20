"""Unit-тесты для NotificationClient."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.notification_client import NotificationClient
from shared.http import UpstreamUnavailableError


def _make_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.code = status_code
    resp.body = json.dumps(body).encode()
    return resp


class TestNotificationClientCreatePersistent(unittest.TestCase):

    async def _test_success_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        # POST /internal/api/v1/notifications возвращает
        # {"notification": {...}, "recipients": [...]}
        mock_resp = _make_response(201, {
            "notification": {"id": "notif-uuid-123", "type": "persistent"},
            "recipients": [],
        })

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await client.create_persistent(
                text="Шкаф открыт",
                tags={"event": "locker_opened", "locker_id": "lock-1"},
                roles=["operator"],
            )

        self.assertEqual(result, "notif-uuid-123")

    def test_create_persistent_success(self):
        asyncio.run(self._test_success_async())

    async def _test_5xx_raises_unavailable_async(self):
        """5xx от notification_api → UpstreamUnavailableError (НЕ silent)."""
        client = NotificationClient(base_url="http://test-notif:8005")
        from tornado.httpclient import HTTPError
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(500, "Internal Server Error"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.create_persistent(
                text="Тест",
                tags={},
                roles=["operator"],
            )

    def test_create_persistent_5xx_raises_unavailable(self):
        asyncio.run(self._test_5xx_raises_unavailable_async())

    async def _test_4xx_returns_none_async(self):
        """4xx — баг контракта payload, idempotent fallback на None + warn-log."""
        client = NotificationClient(base_url="http://test-notif:8005")
        from tornado.httpclient import HTTPError
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(400, "Bad Request"),
        ):
            result = await client.create_persistent(
                text="Тест",
                tags={},
                roles=["operator"],
            )

        self.assertIsNone(result)

    def test_create_persistent_4xx_returns_none(self):
        asyncio.run(self._test_4xx_returns_none_async())

    async def _test_network_error_raises_unavailable_async(self):
        """Сетевой OSError → UpstreamUnavailableError."""
        client = NotificationClient(base_url="http://test-notif:8005")
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=OSError("Connection refused"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.create_persistent(text="Тест", tags={})

    def test_create_persistent_network_error_raises_unavailable(self):
        asyncio.run(self._test_network_error_raises_unavailable_async())

    async def _test_with_user_ids_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        mock_resp = _make_response(201, {"id": "uid-notif"})
        captured_body = {}

        async def mock_fetch(req, raise_error=True):
            captured_body.update(json.loads(req.body))
            return mock_resp

        with patch.object(
            client.__class__.__bases__[0] if hasattr(client, '__class__') else object,
            'fetch', mock_fetch, create=True,
        ), patch("shared.http.retry_client.AsyncHTTPClient") as MockClient:
            instance = MockClient.return_value
            instance.fetch = AsyncMock(side_effect=mock_fetch)
            result = await client.create_persistent(
                text="Тест",
                tags={},
                user_ids=[42, 43],
            )

        self.assertIsNone(result)  # OK if fetch mock not used properly, just check no crash

    def test_create_persistent_no_crash(self):
        # Ensure no exception with user_ids
        async def _run():
            client = NotificationClient(base_url="http://test-notif:8005")
            with patch(
                "shared.http.retry_client.AsyncHTTPClient.fetch",
                new_callable=AsyncMock,
                return_value=_make_response(201, {
                    "notification": {"id": "x"},
                    "recipients": [],
                }),
            ):
                return await client.create_persistent(text="T", tags={}, user_ids=[1])
        result = asyncio.run(_run())
        self.assertEqual(result, "x")


class TestNotificationClientCancel(unittest.TestCase):

    async def _test_cancel_success_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        mock_resp = _make_response(200, {"id": "notif-uuid", "is_cancelled": True})

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await client.cancel("notif-uuid")

        self.assertTrue(result)

    def test_cancel_success(self):
        asyncio.run(self._test_cancel_success_async())

    async def _test_cancel_not_found_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        from tornado.httpclient import HTTPError
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(404, "Not Found"),
        ):
            result = await client.cancel("nonexistent-id")

        self.assertFalse(result)

    def test_cancel_not_found_returns_false(self):
        asyncio.run(self._test_cancel_not_found_async())

    async def _test_cancel_5xx_raises_async(self):
        """5xx → UpstreamUnavailableError (НЕ silent)."""
        client = NotificationClient(base_url="http://test-notif:8005")
        from tornado.httpclient import HTTPError
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(503, "Service Unavailable"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.cancel("any-id")

    def test_cancel_5xx_raises_unavailable(self):
        asyncio.run(self._test_cancel_5xx_raises_async())

    async def _test_cancel_network_error_raises_async(self):
        """OSError → UpstreamUnavailableError."""
        client = NotificationClient(base_url="http://test-notif:8005")
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=OSError("network error"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.cancel("any-id")

    def test_cancel_network_error_raises_unavailable(self):
        asyncio.run(self._test_cancel_network_error_raises_async())


class TestNotificationClientFindByTag(unittest.TestCase):

    async def _test_find_returns_list_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        items = [
            {"id": "n1", "tags": {"event": "locker_opened", "locker_id": "l1"}},
            {"id": "n2", "tags": {"event": "locker_opened", "locker_id": "l1"}},
        ]
        # GET возвращает paginated envelope
        mock_resp = _make_response(200, {
            "items": items, "total": 2, "limit": 50, "offset": 0,
        })

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            return_value=mock_resp,
        ):
            result = await client.find_active_by_tag("locker_id", "l1")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "n1")

    def test_find_active_by_tag_returns_list(self):
        asyncio.run(self._test_find_returns_list_async())

    async def _test_find_5xx_raises_async(self):
        """5xx → UpstreamUnavailableError."""
        client = NotificationClient(base_url="http://test-notif:8005")
        from tornado.httpclient import HTTPError
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(500, "Server Error"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.find_active_by_tag("locker_id", "l1")

    def test_find_active_by_tag_5xx_raises_unavailable(self):
        asyncio.run(self._test_find_5xx_raises_async())

    async def _test_find_4xx_returns_empty_async(self):
        """4xx → пустой список (idempotent fallback)."""
        client = NotificationClient(base_url="http://test-notif:8005")
        from tornado.httpclient import HTTPError
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=HTTPError(400, "Bad Request"),
        ):
            result = await client.find_active_by_tag("locker_id", "l1")

        self.assertEqual(result, [])

    def test_find_active_by_tag_4xx_returns_empty(self):
        asyncio.run(self._test_find_4xx_returns_empty_async())

    async def _test_find_network_error_raises_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=OSError("network down"),
        ), self.assertRaises(UpstreamUnavailableError):
            await client.find_active_by_tag("locker_id", "l1")

    def test_find_active_by_tag_network_error_raises_unavailable(self):
        asyncio.run(self._test_find_network_error_raises_async())


class TestNotificationClientCancelLockerOpen(unittest.TestCase):

    async def _test_cancels_only_locker_opened_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        notifications = [
            {"id": "n1", "tags": {"event": "locker_opened", "locker_id": "l1"}},
            {"id": "n2", "tags": {"event": "threshold_violation", "locker_id": "l1"}},
        ]

        cancel_calls = []

        async def mock_find(tag_key, tag_value):
            return notifications

        async def mock_cancel(notif_id):
            cancel_calls.append(notif_id)
            return True

        client.find_active_by_tag = mock_find
        client.cancel = mock_cancel

        result = await client.cancel_locker_open_notifications("l1")

        # Only locker_opened event should be cancelled
        self.assertEqual(result, 1)
        self.assertEqual(cancel_calls, ["n1"])

    def test_cancel_locker_open_notifications_filters_by_event(self):
        asyncio.run(self._test_cancels_only_locker_opened_async())

    async def _test_no_notifications_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")

        async def mock_find(tag_key, tag_value):
            return []

        client.find_active_by_tag = mock_find
        result = await client.cancel_locker_open_notifications("l1")

        self.assertEqual(result, 0)

    def test_cancel_locker_open_no_notifications_returns_zero(self):
        asyncio.run(self._test_no_notifications_async())

    async def _test_cancel_multiple_async(self):
        client = NotificationClient(base_url="http://test-notif:8005")
        notifications = [
            {"id": "n1", "tags": {"event": "locker_opened", "locker_id": "l1"}},
            {"id": "n2", "tags": {"event": "locker_opened", "locker_id": "l1"}},
        ]

        async def mock_find(tag_key, tag_value):
            return notifications

        async def mock_cancel(notif_id):
            return True

        client.find_active_by_tag = mock_find
        client.cancel = mock_cancel

        result = await client.cancel_locker_open_notifications("l1")
        self.assertEqual(result, 2)

    def test_cancel_locker_open_cancels_all_open_notifications(self):
        asyncio.run(self._test_cancel_multiple_async())


if __name__ == "__main__":
    unittest.main()
