"""Unit-тесты OutboundDispatcher и связанных утилит."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.locker_data.db.repositories.outbound import OutboundEvent
from app.services.outbound_dispatcher import (
    CHANNEL_MEDIA,
    OutboundDispatcher,
    _exp_backoff_seconds,
)


class TestExpBackoff(unittest.TestCase):
    """Backoff растёт экспоненциально и упирается в потолок."""

    def test_first_attempt_uses_base(self):
        self.assertEqual(_exp_backoff_seconds(1, base=2.0), 2.0)

    def test_second_attempt_doubles(self):
        self.assertEqual(_exp_backoff_seconds(2, base=2.0), 4.0)

    def test_third_attempt_quadruples(self):
        self.assertEqual(_exp_backoff_seconds(3, base=2.0), 8.0)

    def test_capped_at_cap(self):
        self.assertEqual(_exp_backoff_seconds(20, base=2.0, cap=600.0), 600.0)

    def test_zero_attempt_safe(self):
        self.assertEqual(_exp_backoff_seconds(0, base=2.0), 2.0)


def _make_event(channel: str = "telemetry", endpoint: str = "/internal/api/v1/telemetry",
                payload: dict | None = None, attempt_count: int = 0) -> OutboundEvent:
    return OutboundEvent(
        id=42,
        channel=channel,
        endpoint=endpoint,
        payload=payload or {"readings": [{"sensor_id": "s", "sensor_type_code": "temperature", "value": 1.0, "locker_id": "l"}]},
        created_at=datetime.now(),
        attempt_count=attempt_count,
        next_attempt_at=datetime.now(),
        last_error=None,
        delivered_at=None,
    )


class TestDispatcherDelivery(unittest.TestCase):
    """Доставка одного события в history_api."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.dispatcher = OutboundDispatcher(
            self.db_mock, history_api_url="http://history_api:8002",
        )
        # repo полностью замокан, чтобы не лезть в БД
        self.dispatcher._repo = MagicMock()
        self.dispatcher._repo.mark_delivered = AsyncMock()
        self.dispatcher._repo.mark_failed = AsyncMock()
        # без сна между HTTP-ретраями
        from shared.http import RetryHTTPClient
        self.dispatcher._http = RetryHTTPClient(backoff_base=0)

    async def _success_async(self):
        event = _make_event()
        captured = {}

        async def fake_fetch(req, raise_error=True):
            captured["url"] = req.url
            captured["method"] = req.method
            captured["body"] = req.body
            captured["headers"] = req.headers
            resp = MagicMock()
            resp.code = 201
            return resp

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock, side_effect=fake_fetch,
        ):
            await self.dispatcher._deliver(event)

        self.assertEqual(captured["url"], "http://history_api:8002/internal/api/v1/telemetry")
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["headers"]["Content-Type"], "application/json")
        self.assertEqual(json.loads(captured["body"].decode()), event.payload)
        self.dispatcher._repo.mark_delivered.assert_awaited_once_with(42)
        self.dispatcher._repo.mark_failed.assert_not_awaited()

    def test_success_marks_delivered(self):
        asyncio.run(self._success_async())

    async def _failure_async(self):
        event = _make_event(attempt_count=2)

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=ConnectionRefusedError("history_api down"),
        ):
            await self.dispatcher._deliver(event)

        self.dispatcher._repo.mark_delivered.assert_not_awaited()
        self.dispatcher._repo.mark_failed.assert_awaited_once()
        # attempt_count=2 → next попытка будет №3, backoff=2*2^2=8s
        args, kwargs = self.dispatcher._repo.mark_failed.call_args
        self.assertEqual(args[0], 42)
        # error string присутствует
        self.assertIn("ConnectionRefusedError", args[1])
        # delay соответствует экспоненциальному backoff
        self.assertEqual(args[2], 8.0)

    def test_failure_marks_failed_with_backoff(self):
        asyncio.run(self._failure_async())


class TestDispatcherMultipart(unittest.TestCase):
    """Сборка multipart для канала media."""

    def test_build_multipart_includes_form_fields_and_file(self):
        import base64
        raw_bytes = b"\x89PNGFAKE"
        payload = {
            "file_bytes_b64": base64.b64encode(raw_bytes).decode("ascii"),
            "file_name": "media-t1.png",
            "file_mime": "image/png",
            "form_fields": {
                "locker_id": "11111111-1111-1111-1111-111111111111",
                "captured_at": "2026-04-29T10:00:00",
                "uploaded_by": None,  # None должен быть пропущен
            },
        }
        body, headers = OutboundDispatcher._build_multipart(payload)

        self.assertTrue(headers["Content-Type"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'name="locker_id"', body)
        self.assertIn(b"11111111-1111-1111-1111-111111111111", body)
        self.assertIn(b'name="captured_at"', body)
        self.assertIn(b"2026-04-29T10:00:00", body)
        # uploaded_by=None должен быть пропущен
        self.assertNotIn(b'name="uploaded_by"', body)
        # file part
        self.assertIn(b'name="file"', body)
        self.assertIn(b"Content-Type: image/png", body)
        self.assertIn(raw_bytes, body)
        # closing boundary
        self.assertTrue(body.rstrip().endswith(b"--"))


class TestDispatcherMediaChannel(unittest.TestCase):
    """Media channel шлёт multipart, а не json."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.dispatcher = OutboundDispatcher(
            self.db_mock, history_api_url="http://history_api:8002",
        )
        self.dispatcher._repo = MagicMock()
        self.dispatcher._repo.mark_delivered = AsyncMock()
        self.dispatcher._repo.mark_failed = AsyncMock()
        from shared.http import RetryHTTPClient
        self.dispatcher._http = RetryHTTPClient(backoff_base=0)

    async def _media_delivery_async(self):
        import base64
        raw_bytes = b"JPEGDATA"
        event = _make_event(
            channel=CHANNEL_MEDIA,
            endpoint="/internal/api/v1/media",
            payload={
                "file_bytes_b64": base64.b64encode(raw_bytes).decode("ascii"),
                "file_name": "media-t1",
                "file_mime": "image/jpeg",
                "form_fields": {"locker_id": "abc", "kind": "photo"},
            },
        )

        captured = {}

        async def fake_fetch(req, raise_error=True):
            captured["url"] = req.url
            captured["headers"] = req.headers
            captured["body"] = req.body
            resp = MagicMock()
            resp.code = 201
            return resp

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock, side_effect=fake_fetch,
        ):
            await self.dispatcher._deliver(event)

        self.assertTrue(
            captured["headers"]["Content-Type"].startswith("multipart/form-data;"),
            f"unexpected content-type: {captured['headers']['Content-Type']}",
        )
        self.assertIn(raw_bytes, captured["body"])
        self.assertIn(b'name="locker_id"', captured["body"])
        self.dispatcher._repo.mark_delivered.assert_awaited_once_with(42)

    def test_media_channel_uses_multipart(self):
        asyncio.run(self._media_delivery_async())


class TestDispatcherHmacSigning(unittest.TestCase):
    """SEC-05: dispatcher подписывает body когда задан hmac_key."""

    def setUp(self):
        self.dispatcher = OutboundDispatcher(
            MagicMock(),
            history_api_url="http://history_api:8002",
            hmac_key=b"test-history-hmac-key",
        )
        self.dispatcher._repo = MagicMock()
        self.dispatcher._repo.mark_delivered = AsyncMock()
        self.dispatcher._repo.mark_failed = AsyncMock()
        from shared.http import RetryHTTPClient
        self.dispatcher._http = RetryHTTPClient(backoff_base=0)

    async def _signed_async(self):
        event = _make_event()
        captured = {}

        async def fake_fetch(req, raise_error=True):
            captured["body"] = req.body
            captured["headers"] = req.headers
            resp = MagicMock()
            resp.code = 201
            return resp

        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock, side_effect=fake_fetch,
        ):
            await self.dispatcher._deliver(event)
        return captured

    def test_signature_headers_present_and_valid(self):
        from shared.utils import body_sha256, compute_request_signature

        captured = asyncio.run(self._signed_async())
        ts = captured["headers"]["X-Internal-Timestamp"]
        sig = captured["headers"]["X-Internal-Signature"]
        # _make_event() по умолчанию шлёт на /internal/api/v1/telemetry —
        # ровно тот path, что dispatcher подписывает (SEC-20).
        expected = compute_request_signature(
            timestamp=ts,
            method="POST",
            path="/internal/api/v1/telemetry",
            body_sha256_hex=body_sha256(captured["body"]),
            key=b"test-history-hmac-key",
        )
        self.assertEqual(sig, expected)

    def test_no_key_means_no_signature(self):
        dispatcher = OutboundDispatcher(
            MagicMock(), history_api_url="http://history_api:8002",
        )
        headers: dict[str, str] = {"Content-Type": "application/json"}
        dispatcher._sign("/internal/api/v1/telemetry", b'{"a":1}', headers)
        self.assertNotIn("X-Internal-Signature", headers)


class TestDispatcherDeadLetter(unittest.TestCase):
    """После max_attempts событие паркуется в dead-letter, а не ретраится вечно."""

    def setUp(self):
        self.dispatcher = OutboundDispatcher(
            MagicMock(), history_api_url="http://history_api:8002", max_attempts=3,
        )
        self.dispatcher._repo = MagicMock()
        self.dispatcher._repo.mark_delivered = AsyncMock()
        self.dispatcher._repo.mark_failed = AsyncMock()
        self.dispatcher._repo.mark_dead_lettered = AsyncMock()
        from shared.http import RetryHTTPClient
        self.dispatcher._http = RetryHTTPClient(backoff_base=0)

    async def _deliver_failing(self, attempt_count: int):
        event = _make_event(attempt_count=attempt_count)
        with patch(
            "shared.http.retry_client.AsyncHTTPClient.fetch",
            new_callable=AsyncMock,
            side_effect=ConnectionRefusedError("history_api down"),
        ):
            await self.dispatcher._deliver(event)

    def test_parks_when_attempts_exhausted(self):
        # attempt_count=2 → next=3 == max_attempts → dead-letter, без mark_failed.
        asyncio.run(self._deliver_failing(2))
        self.dispatcher._repo.mark_dead_lettered.assert_awaited_once()
        self.dispatcher._repo.mark_failed.assert_not_awaited()

    def test_retries_before_exhaustion(self):
        # attempt_count=1 → next=2 < 3 → обычный ретрай (mark_failed), без парковки.
        asyncio.run(self._deliver_failing(1))
        self.dispatcher._repo.mark_failed.assert_awaited_once()
        self.dispatcher._repo.mark_dead_lettered.assert_not_awaited()


class TestDispatcherBatchConcurrency(unittest.TestCase):
    """_deliver_batch доставляет все события батча (конкурентно, с семафором)."""

    def test_delivers_all_events_in_batch(self):
        dispatcher = OutboundDispatcher(
            MagicMock(), history_api_url="http://history_api:8002", concurrency=4,
        )
        dispatcher._repo = MagicMock()
        delivered_ids: list[int] = []

        async def fake_deliver(event):
            delivered_ids.append(event.id)

        dispatcher._deliver = fake_deliver  # type: ignore[assignment]
        events = [_make_event() for _ in range(10)]
        for i, event in enumerate(events):
            event.id = i
        asyncio.run(dispatcher._deliver_batch(events))
        self.assertEqual(sorted(delivered_ids), list(range(10)))

    def test_stop_event_skips_remaining(self):
        # Поднятый stop_event пропускает доставку (graceful-shutdown mid-batch).
        dispatcher = OutboundDispatcher(
            MagicMock(), history_api_url="http://history_api:8002", concurrency=2,
        )
        dispatcher._repo = MagicMock()
        dispatcher._stop_event.set()
        called: list[int] = []

        async def fake_deliver(event):
            called.append(event.id)

        dispatcher._deliver = fake_deliver  # type: ignore[assignment]
        asyncio.run(dispatcher._deliver_batch([_make_event() for _ in range(5)]))
        self.assertEqual(called, [])


class TestProducerEventUidStability(unittest.TestCase):
    """ARC-02: event_uid живёт в payload outbox-строки → стабилен между ретраями.

    Ретрай переиспользует ту же строку (тот же payload), поэтому достаточно
    проверить, что продьюсер кладёт event_uid в payload ОДИН раз при enqueue.
    """

    def test_security_producer_puts_event_uid_in_payload(self):
        from app.services.history_producers.security_event_producer import (
            IntrusionProducer,
        )

        async def run() -> dict:
            prod = IntrusionProducer(MagicMock())
            captured: dict = {}

            async def fake_enqueue(payload, *, delay_seconds=0.0):
                captured["payload"] = payload
                return 1

            prod.enqueue = fake_enqueue  # type: ignore[assignment]
            await prod.publish(
                locker_id="11111111-1111-1111-1111-111111111111",
                sensor_id=None,
                violations=["x"],
            )
            return captured["payload"]

        payload = asyncio.run(run())
        self.assertIn("event_uid", payload)
        # валидный uuid
        import uuid as _uuid
        _uuid.UUID(payload["event_uid"])

    def test_telemetry_producer_puts_per_reading_event_uid(self):
        from app.services.history_producers.telemetry_producer import TelemetryProducer

        async def run() -> dict:
            prod = TelemetryProducer(MagicMock())
            captured: dict = {}

            async def fake_enqueue(payload, *, delay_seconds=0.0):
                captured["payload"] = payload
                return 1

            prod.enqueue = fake_enqueue  # type: ignore[assignment]
            await prod.publish_batch(
                "11111111-1111-1111-1111-111111111111",
                [{"sensor_id": "s", "sensor_type_code": "temperature", "value": 1.0}],
            )
            return captured["payload"]

        payload = asyncio.run(run())
        self.assertIn("event_uid", payload["readings"][0])


class TestBaseHistoryProducer(unittest.TestCase):
    """BaseHistoryProducer ставит payload в outbound queue."""

    def test_requires_channel_and_endpoint(self):
        from app.services.history_producers._base import BaseHistoryProducer

        # Без CHANNEL/ENDPOINT — RuntimeError
        with self.assertRaises(RuntimeError):
            BaseHistoryProducer(MagicMock())

    async def _enqueue_calls_repo_async(self):
        from app.services.history_producers._base import BaseHistoryProducer

        class Dummy(BaseHistoryProducer):
            CHANNEL = "telemetry"
            ENDPOINT = "/internal/api/v1/telemetry"

        prod = Dummy(MagicMock())
        prod._outbound = MagicMock()
        prod._outbound.enqueue = AsyncMock(return_value=99)

        result = await prod.enqueue({"readings": []})

        self.assertEqual(result, 99)
        prod._outbound.enqueue.assert_awaited_once_with(
            "telemetry", "/internal/api/v1/telemetry", {"readings": []},
            delay_seconds=0.0,
        )

    def test_enqueue_calls_repo(self):
        asyncio.run(self._enqueue_calls_repo_async())


if __name__ == "__main__":
    unittest.main()
