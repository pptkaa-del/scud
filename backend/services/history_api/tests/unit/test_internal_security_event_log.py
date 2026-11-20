"""Unit: после записи security event эмитится структурный SIEM-лог с extra-полями."""
from __future__ import annotations

from datetime import UTC, datetime
import logging
import unittest

from app.handlers.internal_security_event import _emit_persisted_log
from app.history_data.db.models.security_event import (
    EventSeverity,
    SecurityEvent,
    SecurityEventType,
)


def _event() -> SecurityEvent:
    return SecurityEvent(
        id=42,
        locker_id="loc-1",
        sensor_id="sen-1",
        event_type=SecurityEventType.DOOR_TAMPER,
        severity=EventSeverity.CRITICAL,
        violated_thresholds=[],
        occurred_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
        resolved_at=None,
        resolved_by=None,
        metadata={},
    )


class TestPersistedSecurityEventLog(unittest.TestCase):
    def test_emits_structured_fields_no_pii(self) -> None:
        with self.assertLogs(
            "app.handlers.internal_security_event", level="INFO",
        ) as cm:
            _emit_persisted_log(_event())
        recs = [r for r in cm.records if getattr(r, "event", None) == "security_event_persisted"]
        self.assertEqual(len(recs), 1)
        rec = recs[0]
        self.assertEqual(rec.id, 42)
        self.assertEqual(rec.type, "door_tamper")
        self.assertEqual(rec.severity, "critical")
        self.assertEqual(rec.locker_id, "loc-1")
        self.assertEqual(rec.sensor_id, "sen-1")
        self.assertEqual(rec.occurred_at, "2026-06-19T12:00:00+00:00")
        self.assertNotIn("phone", rec.__dict__)


if __name__ == "__main__":
    unittest.main()
