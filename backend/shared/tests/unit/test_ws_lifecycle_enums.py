"""Unit: WsCloseCode / WsLifecycleEvent enums + record_ws_event helper.

Проверяет:
- WsCloseCode.SUPERSEDED == 4000
- WsCloseCode является IntEnum (члены равны int-значениям)
- WsLifecycleEvent.SUPERSESSION.value == "supersession"
- record_ws_event(WsLifecycleEvent.SUPERSESSION) инкрементит
  scud_ws_events_total{event="supersession"}
- record_ws_event никогда не кидает (never-raises)
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.enums import WsCloseCode, WsLifecycleEvent
from shared.metrics import (
    SERVICE_NAME,
    record_ws_event,
    ws_events_total,
)


class TestWsCloseCode(unittest.TestCase):
    def test_superseded_value(self) -> None:
        self.assertEqual(WsCloseCode.SUPERSEDED, 4000)

    def test_members_are_ints(self) -> None:
        """IntEnum members must be usable as plain ints (Tornado close(code=...))."""
        self.assertIsInstance(int(WsCloseCode.SUPERSEDED), int)
        self.assertEqual(int(WsCloseCode.SUPERSEDED), 4000)


class TestWsLifecycleEvent(unittest.TestCase):
    def test_supersession_value(self) -> None:
        self.assertEqual(WsLifecycleEvent.SUPERSESSION.value, "supersession")


class TestWsEventsCounter(unittest.TestCase):
    def test_series_exported(self) -> None:
        from prometheus_client import generate_latest
        body = generate_latest().decode()
        self.assertIn("scud_ws_events_total", body)

    def test_labelled_service_and_event(self) -> None:
        self.assertEqual(ws_events_total._labelnames, ("service", "event"))

    def test_record_ws_event_supersession_increments(self) -> None:
        before = ws_events_total.labels(
            service=SERVICE_NAME, event="supersession",
        )._value.get()
        record_ws_event(WsLifecycleEvent.SUPERSESSION)
        after = ws_events_total.labels(
            service=SERVICE_NAME, event="supersession",
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_record_ws_event_never_raises(self) -> None:
        """never-raises: глотаем исключение из .inc()."""
        with patch.object(
            ws_events_total.labels(service=SERVICE_NAME, event="supersession"),
            "inc",
            side_effect=RuntimeError("boom"),
        ):
            record_ws_event(WsLifecycleEvent.SUPERSESSION)  # must not raise


if __name__ == "__main__":
    unittest.main()
