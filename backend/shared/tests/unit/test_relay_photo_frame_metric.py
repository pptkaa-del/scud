"""Unit: scud_relay_photo_frames_total, RelayPhotoMetaKey, DROPPED_RATE_LIMITED."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from prometheus_client import generate_latest

from shared.enums import RelayMessageOutcome, RelayPhotoMetaKey
from shared.metrics import (
    SERVICE_NAME,
    record_relay_photo_frame,
    relay_photo_frames_total,
)


class TestRelayPhotoFrameMetric(unittest.TestCase):
    def test_label_names(self) -> None:
        self.assertEqual(relay_photo_frames_total._labelnames, ("service", "disposition"))

    def test_increments_and_exports(self) -> None:
        series = relay_photo_frames_total.labels(service=SERVICE_NAME, disposition="new_frame")
        before = series._value.get()
        record_relay_photo_frame("new_frame")
        self.assertEqual(series._value.get() - before, 1.0)
        self.assertIn("scud_relay_photo_frames_total", generate_latest().decode())

    def test_never_raises(self) -> None:
        series = relay_photo_frames_total.labels(service=SERVICE_NAME, disposition="duplicate")
        with patch.object(series, "inc", side_effect=RuntimeError("boom")):
            record_relay_photo_frame("duplicate")


class TestRelayPhotoEnums(unittest.TestCase):
    def test_meta_keys(self) -> None:
        self.assertEqual(
            {k.value for k in RelayPhotoMetaKey},
            {"relay_event_id", "photo_id", "photos_cnt", "series_uid"},
        )

    def test_rate_limited_outcome(self) -> None:
        self.assertEqual(RelayMessageOutcome.DROPPED_RATE_LIMITED.value, "dropped_rate_limited")


if __name__ == "__main__":
    unittest.main()
