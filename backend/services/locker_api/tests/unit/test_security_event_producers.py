"""Unit-тесты ThresholdViolation/Intrusion/DoorTamper producers и роутера."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import ANY, AsyncMock, MagicMock, patch

from app.services.history_producers.security_event_producer import (
    CommandLateExecutionProducer,
    DoorTamperProducer,
    IntrusionProducer,
    ThresholdViolationProducer,
    producer_for_sensor_type,
)


def _patch_outbound(producer):
    producer._outbound = MagicMock()
    producer._outbound.enqueue = AsyncMock(return_value=1)
    return producer


class TestProducerRouter(unittest.TestCase):

    def test_default_routes_to_threshold_violation(self):
        prod = producer_for_sensor_type(MagicMock(), "temperature")
        self.assertIsInstance(prod, ThresholdViolationProducer)

    def test_intrusion_routes_to_intrusion_producer(self):
        prod = producer_for_sensor_type(MagicMock(), "intrusion")
        self.assertIsInstance(prod, IntrusionProducer)

    def test_tamper_routes_to_door_tamper_producer(self):
        prod = producer_for_sensor_type(MagicMock(), "tamper")
        self.assertIsInstance(prod, DoorTamperProducer)

    def test_none_routes_to_threshold_violation(self):
        prod = producer_for_sensor_type(MagicMock(), None)
        self.assertIsInstance(prod, ThresholdViolationProducer)

    def test_unknown_routes_to_threshold_violation(self):
        prod = producer_for_sensor_type(MagicMock(), "humidity")
        self.assertIsInstance(prod, ThresholdViolationProducer)


class TestThresholdViolationProducer(unittest.TestCase):
    async def _publish_async(self):
        prod = _patch_outbound(ThresholdViolationProducer(MagicMock()))
        await prod.publish(
            locker_id="l", sensor_id="s", violations=["exceeds 30"],
            sensor_type_code="temperature", raw_value=42.0,
        )
        channel, endpoint, payload = prod._outbound.enqueue.await_args.args
        self.assertEqual(channel, "security_event")
        self.assertEqual(endpoint, "/internal/api/v1/events/security")
        self.assertEqual(payload["event_type"], "threshold_violation")
        self.assertEqual(payload["severity"], "high")
        self.assertEqual(payload["sensor_id"], "s")
        self.assertEqual(payload["violated_thresholds"], [{"description": "exceeds 30"}])
        self.assertEqual(payload["metadata"]["sensor_type_code"], "temperature")
        self.assertEqual(payload["metadata"]["raw_value"], 42.0)

    def test_publish(self):
        asyncio.run(self._publish_async())


class TestIntrusionProducer(unittest.TestCase):
    async def _publish_async(self):
        prod = _patch_outbound(IntrusionProducer(MagicMock()))
        await prod.publish(
            locker_id="l", sensor_id="s", violations=["intrusion triggered"],
            sensor_type_code="intrusion", raw_value=True,
        )
        payload = prod._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["event_type"], "intrusion")
        self.assertEqual(payload["severity"], "high")
        self.assertEqual(payload["metadata"]["sensor_type_code"], "intrusion")
        self.assertEqual(payload["metadata"]["raw_value"], True)

    def test_publish(self):
        asyncio.run(self._publish_async())


class TestExtraMetadata(unittest.TestCase):
    def test_extra_metadata_merged_without_overriding_sensor_code(self):
        prod = _patch_outbound(IntrusionProducer(MagicMock()))
        asyncio.run(prod.publish(
            locker_id="l", sensor_id=None, violations=["x"], sensor_type_code="intrusion",
            extra_metadata={"series_uid": "s1", "photos_cnt": 5,
                            "sensor_type_code": "forged"},
        ))
        meta = prod._outbound.enqueue.await_args.args[2]["metadata"]
        self.assertEqual(meta["series_uid"], "s1")
        self.assertEqual(meta["photos_cnt"], 5)
        self.assertEqual(meta["sensor_type_code"], "intrusion")

    def test_extra_metadata_without_sensor_code(self):
        prod = _patch_outbound(IntrusionProducer(MagicMock()))
        asyncio.run(prod.publish(locker_id="l", sensor_id=None, violations=["x"],
                                 extra_metadata={"relay_event_id": 9}))
        meta = prod._outbound.enqueue.await_args.args[2]["metadata"]
        self.assertEqual(meta, {"relay_event_id": 9})


class TestBuildPayload(unittest.TestCase):
    def test_build_payload_neither_enqueues_nor_counts(self):
        prod = _patch_outbound(IntrusionProducer(MagicMock()))
        with patch(
            "app.services.history_producers.security_event_producer.security_events_total",
        ) as metric:
            payload = prod.build_payload("l", None, ["x"], event_uid="u1")
            metric.labels.assert_not_called()
            prod.record_enqueued()
            metric.labels.assert_called_once_with(
                service=ANY, type="intrusion", severity="high")
        prod._outbound.enqueue.assert_not_called()
        self.assertEqual(payload["event_uid"], "u1")
        self.assertEqual(payload["event_type"], "intrusion")


class TestDoorTamperProducer(unittest.TestCase):
    async def _publish_async(self):
        prod = _patch_outbound(DoorTamperProducer(MagicMock()))
        await prod.publish(
            locker_id="l", sensor_id="s", violations=["tamper triggered"],
            sensor_type_code="tamper", raw_value=True,
        )
        payload = prod._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["event_type"], "door_tamper")
        self.assertEqual(payload["severity"], "critical")

    def test_publish(self):
        asyncio.run(self._publish_async())

    async def _publish_without_sensor_id_async(self):
        prod = _patch_outbound(DoorTamperProducer(MagicMock()))
        await prod.publish(locker_id="l", sensor_id=None, violations=["x"])
        payload = prod._outbound.enqueue.await_args.args[2]
        self.assertNotIn("sensor_id", payload)

    def test_publish_without_sensor_id(self):
        asyncio.run(self._publish_without_sensor_id_async())


class TestCommandLateExecutionProducer(unittest.TestCase):
    def test_late_execution_producer_payload(self):
        prod = _patch_outbound(CommandLateExecutionProducer(MagicMock()))
        asyncio.run(prod.publish(
            "l", None, ["late"], event_uid="uid-1",
            extra_metadata={"late_kind": "replay", "delay_s": 5},
        ))
        payload = prod._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["event_type"], "command_late_execution")
        self.assertEqual(payload["severity"], "high")
        self.assertEqual(payload["event_uid"], "uid-1")
        self.assertEqual(payload["metadata"], {"late_kind": "replay", "delay_s": 5})
        self.assertEqual(payload["violated_thresholds"], [{"description": "late"}])
        self.assertNotIn("sensor_id", payload)


class TestSecurityEventMetricIncrement(unittest.TestCase):
    """publish() инкрементит scud_security_events_total с {type, severity}."""

    async def _publish_increments_async(self, producer_cls, sensor_type, exp_type, exp_sev):
        from shared.metrics import SERVICE_NAME, security_events_total
        before = security_events_total.labels(
            service=SERVICE_NAME, type=exp_type, severity=exp_sev,
        )._value.get()
        prod = _patch_outbound(producer_cls(MagicMock()))
        await prod.publish(
            locker_id="l", sensor_id="s", violations=["x"],
            sensor_type_code=sensor_type,
        )
        after = security_events_total.labels(
            service=SERVICE_NAME, type=exp_type, severity=exp_sev,
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_threshold_increments_high(self):
        asyncio.run(self._publish_increments_async(
            ThresholdViolationProducer, "temperature", "threshold_violation", "high",
        ))

    def test_intrusion_increments_high(self):
        asyncio.run(self._publish_increments_async(
            IntrusionProducer, "intrusion", "intrusion", "high",
        ))

    def test_tamper_increments_critical(self):
        asyncio.run(self._publish_increments_async(
            DoorTamperProducer, "tamper", "door_tamper", "critical",
        ))


if __name__ == "__main__":
    unittest.main()
