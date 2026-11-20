"""Unit-тесты MediaProducer."""

from __future__ import annotations

import asyncio
import base64
import json
from typing import Any
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.services.history_producers.media_producer import MediaProducer
from app.services.media_buffer import MediaTransferMeta, MediaTransferResult


def _meta(**overrides: Any) -> MediaTransferMeta:
    fields: dict[str, Any] = {
        "transfer_id": "t1",
        "kind": "photo",
        "mime_type": "image/jpeg",
        "size_bytes": 4,
        "captured_at": "2026-06-01T12:00:00Z",
        "sensor_id": None,
        "width": None,
        "height": None,
        "duration_ms": None,
    }
    fields.update(overrides)
    return MediaTransferMeta(**fields)


class TestMediaProducer(unittest.TestCase):
    def setUp(self) -> None:
        self.db = MagicMock()
        self.db.execute = AsyncMock()
        self.producer = MediaProducer(self.db)
        self.producer._outbound = MagicMock()
        self.producer._outbound.enqueue = AsyncMock(return_value=1)

    async def _publish(self) -> None:
        await self.producer.publish(
            result=MediaTransferResult(meta=_meta(width=10, height=10),
                                       bytes_=b"\xff\xd8\xff\xe0"),
            locker_id="55555555-5555-5555-5555-555555555555",
            security_event_id=7,
        )
        channel, endpoint, payload = self.producer._outbound.enqueue.await_args.args
        self.assertEqual(channel, "media")
        self.assertEqual(endpoint, "/internal/api/v1/media")
        ff = payload["form_fields"]
        self.assertEqual(ff["locker_id"], "55555555-5555-5555-5555-555555555555")
        self.assertEqual(ff["security_event_id"], "7")
        self.assertEqual(ff["kind"], "photo")
        self.assertEqual(ff["mime_type"], "image/jpeg")
        self.assertEqual(ff["width"], "10")
        self.assertEqual(ff["height"], "10")
        self.assertEqual(json.loads(ff["metadata"]), {})
        self.assertNotIn("security_event_uid", ff)
        self.assertNotIn("event_sensor_id", ff)
        self.assertNotIn("event_occurred_at", ff)
        self.assertEqual(base64.b64decode(payload["file_bytes_b64"]), b"\xff\xd8\xff\xe0")

    def test_publish_enqueues_media_row(self) -> None:
        asyncio.run(self._publish())


class _CapturingProducer(MediaProducer):
    def __init__(self) -> None:
        self.enqueued: list[tuple[dict[str, Any], float]] = []

    async def enqueue(  # type: ignore[override]
        self, payload: dict[str, Any], *, delay_seconds: float = 0.0
    ) -> int:
        self.enqueued.append((payload, delay_seconds))
        return 1


def _result(**overrides: Any) -> MediaTransferResult:
    return MediaTransferResult(meta=_meta(transfer_id="relay-1-7", **overrides), bytes_=b"abc")


def test_explicit_event_uid_is_used() -> None:
    p = _CapturingProducer()
    asyncio.run(p.publish(result=_result(), locker_id="L1", security_event_id=None,
                          event_uid="11111111-1111-5111-8111-111111111111"))
    payload, _ = p.enqueued[0]
    assert payload["form_fields"]["event_uid"] == "11111111-1111-5111-8111-111111111111"


def test_default_event_uid_is_minted() -> None:
    p = _CapturingProducer()
    asyncio.run(p.publish(result=_result(), locker_id="L1", security_event_id=None))
    payload, _ = p.enqueued[0]
    assert len(payload["form_fields"]["event_uid"]) == 36


def test_metadata_and_security_event_uid_are_forwarded_without_delay() -> None:
    p = _CapturingProducer()
    meta = {"relay_event_id": 42, "photo_id": 2, "photos_cnt": 5, "series_uid": "s"}
    asyncio.run(p.publish(
        result=_result(security_event_uid="22222222-2222-5222-8222-222222222222",
                       metadata=meta),
        locker_id="L1", security_event_id=None,
    ))
    payload, delay = p.enqueued[0]
    ff = payload["form_fields"]
    assert json.loads(ff["metadata"]) == meta
    assert ff["security_event_uid"] == "22222222-2222-5222-8222-222222222222"
    assert delay == 0.0


if __name__ == "__main__":
    unittest.main()
