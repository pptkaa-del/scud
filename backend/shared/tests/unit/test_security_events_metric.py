"""Unit: регистрация и инкремент scud_security_events_total в shared.metrics."""
from __future__ import annotations

import unittest

from prometheus_client import generate_latest

from shared.metrics import SERVICE_NAME, security_events_total


class TestSecurityEventsMetric(unittest.TestCase):
    def test_counter_is_exported_with_expected_name(self) -> None:
        # Имя серии в exposition-формате — scud_security_events_total.
        body = generate_latest().decode()
        self.assertIn("scud_security_events_total", body)

    def test_increment_with_type_and_severity_labels(self) -> None:
        before = security_events_total.labels(
            service=SERVICE_NAME, type="door_tamper", severity="critical",
        )._value.get()
        security_events_total.labels(
            service=SERVICE_NAME, type="door_tamper", severity="critical",
        ).inc()
        after = security_events_total.labels(
            service=SERVICE_NAME, type="door_tamper", severity="critical",
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_label_order_is_service_type_severity(self) -> None:
        # Контракт: service первым, затем type, severity (низкая кардинальность).
        self.assertEqual(
            security_events_total._labelnames, ("service", "type", "severity"),
        )


if __name__ == "__main__":
    unittest.main()
