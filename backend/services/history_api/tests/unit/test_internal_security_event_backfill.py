"""Unit: запись security event дотягивает к нему кадры, пришедшие раньше события."""
from __future__ import annotations

from datetime import UTC, datetime
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from tornado.testing import AsyncHTTPTestCase

from app.handlers.internal_security_event import InternalSecurityEventHandler
from app.history_data.db.models.security_event import (
    EventSeverity,
    SecurityEvent,
    SecurityEventType,
)
from app.history_data.db.repositories.media_repository import MediaRepository
from app.history_data.db.repositories.security_event_repository import (
    SecurityEventRepository,
)
from app.main import make_app


def _event() -> SecurityEvent:
    return SecurityEvent(
        id=1,
        locker_id=str(uuid4()),
        sensor_id=None,
        event_type=SecurityEventType.INTRUSION,
        severity=EventSeverity.HIGH,
        violated_thresholds=[],
        occurred_at=datetime(2026, 6, 19, 12, 0, 0, tzinfo=UTC),
        resolved_at=None,
        resolved_by=None,
        metadata={},
    )


class TestSecurityEventMediaBackfill(AsyncHTTPTestCase):
    def get_app(self):
        self.calls = MagicMock()
        self.calls.create = AsyncMock(return_value=_event())
        self.calls.link_pending_to_event = AsyncMock()
        original_set_status = InternalSecurityEventHandler.set_status

        def _spy_set_status(handler, status_code, reason=None):
            self.calls.set_status(status_code)
            return original_set_status(handler, status_code, reason)

        self.patches = [
            patch.object(SecurityEventRepository, "create", self.calls.create),
            patch.object(MediaRepository, "link_pending_to_event",
                         self.calls.link_pending_to_event),
            patch.object(InternalSecurityEventHandler, "set_status", _spy_set_status),
        ]
        for p in self.patches:
            p.start()
        return make_app(MagicMock())

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def _post(self, event_uid: str | None):
        body: dict[str, object] = {
            "locker_id": str(uuid4()),
            "event_type": "intrusion",
            "severity": "high",
        }
        if event_uid is not None:
            body["event_uid"] = event_uid
        return self.fetch(
            "/internal/api/v1/events/security", method="POST",
            body=json.dumps(body), headers={"Content-Type": "application/json"},
        )

    def _names(self) -> list[str]:
        return [c[0] for c in self.calls.mock_calls]

    def test_created_backfills_before_201(self):
        uid = str(uuid4())
        response = self._post(uid)
        self.assertEqual(response.code, 201)
        self.calls.link_pending_to_event.assert_awaited_once_with(uid)
        names = self._names()
        self.assertLess(names.index("link_pending_to_event"), names.index("set_status"))

    def test_duplicate_backfills_then_200(self):
        self.calls.create.return_value = None
        uid = str(uuid4())
        response = self._post(uid)
        self.assertEqual(response.code, 200)
        self.calls.link_pending_to_event.assert_awaited_once_with(uid)
        names = self._names()
        self.assertLess(names.index("link_pending_to_event"), names.index("set_status"))

    def test_backfill_failure_500(self):
        self.calls.link_pending_to_event.side_effect = RuntimeError("db down")
        response = self._post(str(uuid4()))
        self.assertEqual(response.code, 500)

    def test_backfill_failure_keeps_persisted_log(self):
        """Ретрай увидит дубликат и лог не напишет — значит, его пишем до привязки."""
        self.calls.link_pending_to_event.side_effect = RuntimeError("db down")
        with self.assertLogs("app.handlers.internal_security_event", "INFO") as logs:
            response = self._post(str(uuid4()))
        self.assertEqual(response.code, 500)
        persisted = [r for r in logs.records if getattr(r, "event", None) == "security_event_persisted"]
        self.assertEqual(len(persisted), 1)

    def test_no_event_uid_no_backfill(self):
        response = self._post(None)
        self.assertEqual(response.code, 201)
        self.calls.link_pending_to_event.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
