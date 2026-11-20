"""Unit-тесты TelemetryProducer."""

from __future__ import annotations

import asyncio
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.history_producers.telemetry_producer import TelemetryProducer


def _make_producer():
    db_mock = MagicMock()
    producer = TelemetryProducer(db_mock)
    producer._outbound = MagicMock()
    producer._outbound.enqueue = AsyncMock(return_value=1)
    return producer


class TestTelemetryProducer(unittest.TestCase):

    async def _empty_batch_no_enqueue_async(self):
        producer = _make_producer()
        result = await producer.publish_batch(locker_id="l", readings=[])
        self.assertIsNone(result)
        producer._outbound.enqueue.assert_not_called()

    def test_empty_batch_no_enqueue(self):
        asyncio.run(self._empty_batch_no_enqueue_async())

    async def _adds_locker_id_to_each_reading_async(self):
        producer = _make_producer()
        await producer.publish_batch(
            locker_id="locker-1",
            readings=[
                {"sensor_id": "s1", "sensor_type_code": "temperature", "value": 22.0},
                {"sensor_id": "s2", "sensor_type_code": "humidity", "value": 60.0},
            ],
        )

        producer._outbound.enqueue.assert_awaited_once()
        channel, endpoint, payload = producer._outbound.enqueue.await_args.args
        self.assertEqual(channel, "telemetry")
        self.assertEqual(endpoint, "/internal/api/v1/telemetry")
        self.assertEqual(payload["locker_id"], "locker-1")
        self.assertEqual(len(payload["readings"]), 2)
        for reading in payload["readings"]:
            self.assertEqual(reading["locker_id"], "locker-1")

    def test_adds_locker_id_to_each_reading(self):
        asyncio.run(self._adds_locker_id_to_each_reading_async())

    async def _default_recorded_at_async(self):
        producer = _make_producer()
        await producer.publish_batch(
            locker_id="l",
            readings=[{"sensor_id": "s", "sensor_type_code": "temperature", "value": 1.0}],
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        recorded = payload["readings"][0]["recorded_at"]
        # ISO 8601, парсится datetime'ом
        datetime.fromisoformat(recorded)

    def test_default_recorded_at(self):
        asyncio.run(self._default_recorded_at_async())

    async def _explicit_recorded_at_preserved_async(self):
        producer = _make_producer()
        explicit = datetime(2026, 1, 2, 3, 4, 5)
        await producer.publish_batch(
            locker_id="l",
            readings=[{
                "sensor_id": "s",
                "sensor_type_code": "temperature",
                "value": 1.0,
                "recorded_at": explicit,
            }],
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["readings"][0]["recorded_at"], explicit.isoformat())

    def test_explicit_recorded_at_preserved(self):
        asyncio.run(self._explicit_recorded_at_preserved_async())


if __name__ == "__main__":
    unittest.main()
