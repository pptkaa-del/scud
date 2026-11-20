"""Unit-тесты CommandHistoryProducer."""

from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.history_producers.command_history_producer import CommandHistoryProducer


def _make_producer():
    db_mock = MagicMock()
    producer = CommandHistoryProducer(db_mock)
    producer._outbound = MagicMock()
    producer._outbound.enqueue = AsyncMock(return_value=1)
    return producer


class TestCommandHistoryProducer(unittest.TestCase):

    async def _publish_created_async(self):
        producer = _make_producer()
        await producer.publish_created(
            command_id="cmd-1",
            locker_id="locker-1",
            command_type="open",
            parameters={"timeout": 30},
            created_by=42,
            locker_inventory_number="СО №1",
        )
        channel, endpoint, payload = producer._outbound.enqueue.await_args.args
        self.assertEqual(channel, "command_history")
        self.assertEqual(endpoint, "/internal/api/v1/commands/history")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["command_id"], "cmd-1")
        self.assertEqual(payload["locker_id"], "locker-1")
        self.assertEqual(payload["command_type"], "open")
        self.assertEqual(payload["parameters"], {"timeout": 30})
        self.assertEqual(payload["created_by"], 42)
        self.assertEqual(payload["locker_inventory_number"], "СО №1")
        self.assertNotIn("result", payload)
        self.assertNotIn("error_message", payload)

    def test_publish_created(self):
        asyncio.run(self._publish_created_async())

    async def _publish_sent_async(self):
        producer = _make_producer()
        await producer.publish_sent(
            command_id="cmd-2", locker_id="l", command_type="close",
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["status"], "sent")

    def test_publish_sent(self):
        asyncio.run(self._publish_sent_async())

    async def _publish_executed_includes_result_async(self):
        producer = _make_producer()
        await producer.publish_executed(
            command_id="cmd-3", locker_id="l", command_type="open",
            result={"door": "opened"},
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["status"], "executed")
        self.assertEqual(payload["result"], {"door": "opened"})

    def test_publish_executed_includes_result(self):
        asyncio.run(self._publish_executed_includes_result_async())

    async def _publish_failed_includes_error_async(self):
        producer = _make_producer()
        await producer.publish_failed(
            command_id="cmd-4", locker_id="l", command_type="open",
            error_message="Device unreachable",
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["error_message"], "Device unreachable")

    def test_publish_failed_includes_error(self):
        asyncio.run(self._publish_failed_includes_error_async())

    async def _omits_none_optional_fields_async(self):
        producer = _make_producer()
        await producer.publish_created(
            command_id="cmd-5", locker_id="l", command_type="reboot",
        )
        payload = producer._outbound.enqueue.await_args.args[2]
        self.assertNotIn("created_by", payload)
        self.assertNotIn("user_phone", payload)
        self.assertNotIn("user_full_name", payload)
        self.assertNotIn("locker_inventory_number", payload)
        self.assertNotIn("parameters", payload)

    def test_omits_none_optional_fields(self):
        asyncio.run(self._omits_none_optional_fields_async())


if __name__ == "__main__":
    unittest.main()
