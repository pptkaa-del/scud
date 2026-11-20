"""Unit: scud_command_pending_total gauge и never-raises сеттер в shared.metrics."""
from __future__ import annotations

import unittest

from prometheus_client import generate_latest

from shared.metrics import (
    SERVICE_NAME,
    command_pending_total,
    set_command_pending,
)


class TestCommandPendingMetric(unittest.TestCase):
    def test_series_exported(self) -> None:
        body = generate_latest().decode()
        self.assertIn("scud_command_pending_total", body)

    def test_labelled_service_only(self) -> None:
        self.assertEqual(command_pending_total._labelnames, ("service",))

    def test_set_command_pending_sets_value(self) -> None:
        set_command_pending(5)
        self.assertEqual(
            command_pending_total.labels(service=SERVICE_NAME)._value.get(), 5.0,
        )

    def test_set_command_pending_zero(self) -> None:
        set_command_pending(0)
        self.assertEqual(
            command_pending_total.labels(service=SERVICE_NAME)._value.get(), 0.0,
        )

    def test_set_command_pending_never_raises_on_string(self) -> None:
        # Non-int input glотается — метрика не должна валить domain path.
        set_command_pending("x")  # type: ignore[arg-type]
        # No exception → pass.

    def test_set_command_pending_ignores_bool(self) -> None:
        # bool — подкласс int; isinstance-чек фильтрует его явно.
        # mypy считает bool совместимым с int, поэтому type: ignore не нужен.
        set_command_pending(True)
        # No exception → pass (gauge не обновляется).

    def test_set_command_pending_ignores_none(self) -> None:
        set_command_pending(None)  # type: ignore[arg-type]
        # No exception → pass.


if __name__ == "__main__":
    unittest.main()
