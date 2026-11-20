"""Unit: вход в ALARM эмитит структурную SIEM-лог-запись с extra-полями, без PII."""
from __future__ import annotations

import asyncio
import logging
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.locker_data.db.models.locker import Connectivity, DoorState, OperationalStatus
from app.services.state_monitor import StateMonitor


def _mock_locker():
    locker = MagicMock()
    locker.connectivity = Connectivity.ONLINE
    locker.door = DoorState.CLOSED
    locker.operational_status = OperationalStatus.NORMAL
    return locker


class TestAlarmStructuredLog(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()

    async def _run(self, sensor_code):
        sm = StateMonitor(self.db)
        sm._send_notification = AsyncMock()
        sm._log_alarm_event = AsyncMock()
        with patch(
            "app.locker_data.db.repositories.locker.LockerRepository",
        ) as repo_mock, self.assertLogs("app.services.state_monitor", level="WARNING") as cm:
            repo_mock.return_value.get_by_id = AsyncMock(return_value=_mock_locker())
            repo_mock.return_value.update_operational_status = AsyncMock()
            await sm.handle_threshold_violation(
                "loc-1", "sen-1", ["x"], sensor_code=sensor_code,
            )
        return cm.records

    def test_tamper_logs_critical_with_fields_no_pii(self):
        records = asyncio.run(self._run("tamper"))
        alarm = [r for r in records if getattr(r, "event", None) == "security_alarm"]
        self.assertEqual(len(alarm), 1)
        rec = alarm[0]
        self.assertEqual(rec.levelno, logging.CRITICAL)
        self.assertEqual(rec.type, "door_tamper")
        self.assertEqual(rec.severity, "critical")
        self.assertEqual(rec.locker_id, "loc-1")
        self.assertEqual(rec.sensor_id, "sen-1")
        # Без PII: ни телефона, ни имени в сообщении/полях.
        self.assertNotIn("phone", rec.__dict__)

    def test_non_tamper_logs_warning_high(self):
        records = asyncio.run(self._run("temperature"))
        alarm = [r for r in records if getattr(r, "event", None) == "security_alarm"]
        self.assertEqual(len(alarm), 1)
        self.assertEqual(alarm[0].levelno, logging.WARNING)
        self.assertEqual(alarm[0].type, "threshold_violation")
        self.assertEqual(alarm[0].severity, "high")


if __name__ == "__main__":
    unittest.main()
