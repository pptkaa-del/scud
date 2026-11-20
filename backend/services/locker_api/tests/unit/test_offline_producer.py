"""Unit-тесты OfflineEventProducer."""

from __future__ import annotations

import asyncio
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock
import uuid

from app.services.history_producers.offline_producer import (
    REASON_HEARTBEAT_TIMEOUT,
    OfflineEventProducer,
)


def _make_producer():
    db_mock = MagicMock()
    producer = OfflineEventProducer(db_mock)
    producer._outbound = MagicMock()
    producer._outbound.enqueue = AsyncMock(return_value=1)
    return producer


class TestOfflineEventProducer(unittest.TestCase):

    async def _publish_offline_basic_async(self):
        producer = _make_producer()
        last_seen = datetime(2026, 4, 29, 10, 0, 0)
        await producer.publish_offline(
            locker_id="locker-1",
            reason=REASON_HEARTBEAT_TIMEOUT,
            last_seen_at=last_seen,
        )
        channel, endpoint, payload = producer._outbound.enqueue.await_args.args
        self.assertEqual(channel, "security_event")
        self.assertEqual(endpoint, "/internal/api/v1/events/security")
        self.assertEqual(payload["locker_id"], "locker-1")
        self.assertEqual(payload["event_type"], "offline")
        self.assertEqual(payload["severity"], "medium")
        self.assertEqual(payload["metadata"]["reason"], "heartbeat_timeout")
        self.assertEqual(payload["metadata"]["last_seen_at"], last_seen.isoformat())

    def test_publish_offline_basic(self):
        asyncio.run(self._publish_offline_basic_async())

    async def _publish_without_last_seen_async(self):
        producer = _make_producer()
        await producer.publish_offline(
            locker_id="l", reason=REASON_HEARTBEAT_TIMEOUT,
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["metadata"], {"reason": "heartbeat_timeout"})

    def test_publish_without_last_seen(self):
        asyncio.run(self._publish_without_last_seen_async())

    async def _rejects_unknown_reason_async(self):
        producer = _make_producer()
        with self.assertRaises(ValueError):
            await producer.publish_offline(locker_id="l", reason="bogus")
        producer._outbound.enqueue.assert_not_called()

    def test_rejects_unknown_reason(self):
        asyncio.run(self._rejects_unknown_reason_async())

    async def _puts_event_uid_in_payload_async(self):
        # ARC-02: продьюсер кладёт стабильный event_uid в payload outbox-строки
        # НА enqueue — чтобы offline replay/ретрай доставки дедуплицировался
        # через ON CONFLICT по security_events(event_uid).
        producer = _make_producer()
        await producer.publish_offline(
            locker_id="locker-1", reason=REASON_HEARTBEAT_TIMEOUT,
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        self.assertIn("event_uid", payload)
        # Это валидный uuid4-строкой (выбросит ValueError, если нет).
        uuid.UUID(payload["event_uid"])

    def test_puts_event_uid_in_payload(self):
        asyncio.run(self._puts_event_uid_in_payload_async())


class TestOfflineMetricIncrement(unittest.TestCase):
    """publish_offline() инкрементит scud_security_events_total{offline, medium}."""

    async def _increments_async(self):
        from shared.metrics import SERVICE_NAME, security_events_total
        before = security_events_total.labels(
            service=SERVICE_NAME, type="offline", severity="medium",
        )._value.get()
        producer = _make_producer()
        await producer.publish_offline(
            locker_id="l", reason=REASON_HEARTBEAT_TIMEOUT,
        )
        after = security_events_total.labels(
            service=SERVICE_NAME, type="offline", severity="medium",
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_publish_offline_increments(self):
        asyncio.run(self._increments_async())

    async def _bad_reason_does_not_increment_async(self):
        from shared.metrics import SERVICE_NAME, security_events_total
        before = security_events_total.labels(
            service=SERVICE_NAME, type="offline", severity="medium",
        )._value.get()
        producer = _make_producer()
        with self.assertRaises(ValueError):
            await producer.publish_offline(locker_id="l", reason="bogus")
        after = security_events_total.labels(
            service=SERVICE_NAME, type="offline", severity="medium",
        )._value.get()
        self.assertEqual(after - before, 0.0)

    def test_bad_reason_does_not_increment(self):
        asyncio.run(self._bad_reason_does_not_increment_async())


if __name__ == "__main__":
    unittest.main()
