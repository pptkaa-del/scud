"""Unit: scud_mqtt_relay_messages_total counter и record_relay_message helper.

Проверяет:
- record_relay_message(RelayMessageOutcome.PROCESSED) инкрементирует
  scud_mqtt_relay_messages_total{service, outcome="processed"}
- record_relay_message никогда не кидает (патчим .inc() с side_effect=Exception)
- Метрика экспортируется в Prometheus exposition format
- label-схема: ["service", "outcome"]
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from prometheus_client import generate_latest

from shared.enums import RelayMessageOutcome
from shared.metrics import (
    SERVICE_NAME,
    mqtt_relay_messages_total,
    record_relay_message,
)


class TestRelayMessageMetric(unittest.TestCase):
    def test_series_exported(self) -> None:
        # Touch the counter first so the series is initialised before asserting
        # export — removes test-order dependence on other tests.
        record_relay_message(RelayMessageOutcome.PROCESSED)
        body = generate_latest().decode()
        self.assertIn("scud_mqtt_relay_messages_total", body)

    def test_label_names(self) -> None:
        self.assertEqual(
            mqtt_relay_messages_total._labelnames, ("service", "outcome")
        )

    def test_processed_increments(self) -> None:
        before = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME, outcome=RelayMessageOutcome.PROCESSED.value
        )._value.get()
        record_relay_message(RelayMessageOutcome.PROCESSED)
        after = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME, outcome=RelayMessageOutcome.PROCESSED.value
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_dropped_malformed_increments(self) -> None:
        before = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME,
            outcome=RelayMessageOutcome.DROPPED_MALFORMED.value,
        )._value.get()
        record_relay_message(RelayMessageOutcome.DROPPED_MALFORMED)
        after = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME,
            outcome=RelayMessageOutcome.DROPPED_MALFORMED.value,
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_dropped_unknown_relay_increments(self) -> None:
        before = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME,
            outcome=RelayMessageOutcome.DROPPED_UNKNOWN_RELAY.value,
        )._value.get()
        record_relay_message(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY)
        after = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME,
            outcome=RelayMessageOutcome.DROPPED_UNKNOWN_RELAY.value,
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_handler_error_increments(self) -> None:
        before = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME,
            outcome=RelayMessageOutcome.HANDLER_ERROR.value,
        )._value.get()
        record_relay_message(RelayMessageOutcome.HANDLER_ERROR)
        after = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME,
            outcome=RelayMessageOutcome.HANDLER_ERROR.value,
        )._value.get()
        self.assertEqual(after - before, 1.0)

    def test_never_raises_on_inc_failure(self) -> None:
        label_set = mqtt_relay_messages_total.labels(
            service=SERVICE_NAME, outcome=RelayMessageOutcome.PROCESSED.value
        )
        with patch.object(label_set, "inc", side_effect=RuntimeError("boom")):
            record_relay_message(RelayMessageOutcome.PROCESSED)  # must not raise


if __name__ == "__main__":
    unittest.main()
