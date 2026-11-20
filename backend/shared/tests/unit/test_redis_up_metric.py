"""Unit: scud_redis_up gauge и never-raises сеттер в shared.metrics."""
from __future__ import annotations

import unittest

from prometheus_client import generate_latest

from shared.metrics import (
    SERVICE_NAME,
    redis_up,
    set_redis_up,
)


class TestRedisUpMetric(unittest.TestCase):
    def test_series_exported(self) -> None:
        body = generate_latest().decode()
        self.assertIn("scud_redis_up", body)

    def test_labelled_service_only(self) -> None:
        self.assertEqual(redis_up._labelnames, ("service",))

    def test_set_redis_up_true_sets_one(self) -> None:
        set_redis_up(True)
        self.assertEqual(
            redis_up.labels(service=SERVICE_NAME)._value.get(), 1.0,
        )

    def test_set_redis_up_false_sets_zero(self) -> None:
        set_redis_up(False)
        self.assertEqual(
            redis_up.labels(service=SERVICE_NAME)._value.get(), 0.0,
        )

    def test_set_redis_up_never_raises_on_bad_input(self) -> None:
        # Production-flow не должен падать из-за метрик: нечисловой/None вход
        # глотается (зеркалит set_mqtt_connected).
        set_redis_up(None)        # type: ignore[arg-type]
        set_redis_up(object())    # type: ignore[arg-type]
        # No exception → pass.


if __name__ == "__main__":
    unittest.main()
