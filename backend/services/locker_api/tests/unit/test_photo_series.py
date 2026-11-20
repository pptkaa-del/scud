"""Unit: классификация кадров relay-фото и детерминированные uid серии."""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import uuid

import pytest

from app.locker_data.db.models.relay_photo_series import RelayPhotoSeries
from app.mqtt.photo_series import (
    RELAY_PHOTO_NS,
    FrameDisposition,
    classify_frame,
    frame_digest,
    frame_media_uid,
    frame_metadata,
    series_intrusion_uid,
    validate_frame,
)
from app.mqtt.relay_codec import Photo

JPEG_A = b"\xff\xd8\xff\xe0AAAA\xff\xd9"
JPEG_B = b"\xff\xd8\xff\xe0BBBB\xff\xd9"
_NOW = datetime(2026, 10, 4, tzinfo=UTC)


def _series(*, sha: dict[str, str] | None = None, fresh: bool = True,
            superseded: bool = False, sid: int = 1) -> RelayPhotoSeries:
    return RelayPhotoSeries(
        id=sid, locker_id="L1", relay_id=1001, event_id=42,
        series_uid=str(uuid.uuid4()), photos_cnt=3, frame_sha=dict(sha or {}),
        total_bytes=0, first_seen_at=_NOW, last_frame_at=_NOW,
        intrusion_enqueued_at=None,
        superseded_at=_NOW if superseded else None, fresh=fresh,
    )


def _photo(photo_id: int = 1, photos_cnt: int = 3, body: bytes = JPEG_A) -> Photo:
    return Photo(device_id=1001, event_id=42, photos_cnt=photos_cnt,
                 photo_id=photo_id, photo_size=len(body), photo=body)


class TestClassify:
    def test_new_series_when_no_rows(self):
        cls = classify_frame([], 1, frame_digest(JPEG_A))
        assert cls.disposition is FrameDisposition.NEW_SERIES
        assert cls.series is None

    def test_new_frame_into_current_series(self):
        current = _series(sha={"1": frame_digest(JPEG_A)})
        cls = classify_frame([current], 2, frame_digest(JPEG_B))
        assert cls.disposition is FrameDisposition.NEW_FRAME
        assert cls.series is current

    def test_duplicate_same_sha(self):
        current = _series(sha={"1": frame_digest(JPEG_A)})
        cls = classify_frame([current], 1, frame_digest(JPEG_A))
        assert cls.disposition is FrameDisposition.DUPLICATE
        assert cls.series is current

    def test_duplicate_after_complete_series(self):
        sha = {"1": frame_digest(JPEG_A), "2": frame_digest(JPEG_B)}
        current = _series(sha=sha)
        assert classify_frame([current], 2, frame_digest(JPEG_B)).disposition \
            is FrameDisposition.DUPLICATE

    @pytest.mark.parametrize("tail", [b"\x00" * 100, b"\xff" * 37, b"\x00\xff\x00"])
    def test_duplicate_with_different_padding_tail(self, tail):
        current = _series(sha={"1": frame_digest(JPEG_A)})
        cls = classify_frame([current], 1, frame_digest(JPEG_A + tail))
        assert cls.disposition is FrameDisposition.DUPLICATE

    def test_reset_on_sha_conflict(self):
        current = _series(sha={"1": frame_digest(JPEG_A)})
        cls = classify_frame([current], 1, frame_digest(JPEG_B))
        assert cls.disposition is FrameDisposition.RESET
        assert cls.series is current

    def test_duplicate_of_fresh_superseded_series_does_not_flap(self):
        old = _series(sha={"1": frame_digest(JPEG_A)}, superseded=True, sid=1)
        current = _series(sha={"1": frame_digest(JPEG_B)}, sid=2)
        cls = classify_frame([current, old], 1, frame_digest(JPEG_A))
        assert cls.disposition is FrameDisposition.DUPLICATE
        assert cls.series is old

    def test_new_frame_into_stale_series_resets(self):
        stale = _series(sha={"1": frame_digest(JPEG_A)}, fresh=False)
        cls = classify_frame([stale], 2, frame_digest(JPEG_B))
        assert cls.disposition is FrameDisposition.RESET
        assert cls.series is stale

    def test_duplicate_of_stale_row_resets(self):
        stale = _series(sha={"1": frame_digest(JPEG_A)}, fresh=False)
        cls = classify_frame([stale], 1, frame_digest(JPEG_A))
        assert cls.disposition is FrameDisposition.RESET


class TestValidate:
    @pytest.mark.parametrize("photo_id", [0, 17])
    def test_photo_id_out_of_range(self, photo_id):
        with pytest.raises(ValueError):
            validate_frame(_photo(photo_id=photo_id))

    @pytest.mark.parametrize("photos_cnt", [0, 17])
    def test_photos_cnt_out_of_range(self, photos_cnt):
        with pytest.raises(ValueError):
            validate_frame(_photo(photos_cnt=photos_cnt))

    def test_missing_soi(self):
        with pytest.raises(ValueError):
            validate_frame(_photo(body=b"\x00\x00" + JPEG_A))

    def test_zero_tail_after_eoi_ok(self):
        validate_frame(_photo(body=JPEG_A + b"\x00" * 64))

    def test_bounds_ok(self):
        validate_frame(_photo(photo_id=16, photos_cnt=16))


class TestDigest:
    def test_without_eoi_strips_padding(self):
        body = b"\xff\xd8\xff\xe0trunc"
        assert frame_digest(body + b"\x00\xff\x00") == \
            hashlib.sha256(body).hexdigest()

    def test_with_eoi_ignores_trailer(self):
        assert frame_digest(JPEG_A + b"\x00junk") == hashlib.sha256(JPEG_A).hexdigest()

    def test_distinct_frames_distinct_digest(self):
        assert frame_digest(JPEG_A) != frame_digest(JPEG_B)


class TestUids:
    def test_deterministic(self):
        s = str(uuid.uuid4())
        assert series_intrusion_uid(s) == series_intrusion_uid(s)
        assert frame_media_uid(s, 3) == frame_media_uid(s, 3)

    def test_distinct_per_series_and_frame(self):
        a, b = str(uuid.uuid4()), str(uuid.uuid4())
        assert series_intrusion_uid(a) != series_intrusion_uid(b)
        assert frame_media_uid(a, 1) != frame_media_uid(b, 1)
        assert frame_media_uid(a, 1) != frame_media_uid(a, 2)
        assert frame_media_uid(a, 1) != series_intrusion_uid(a)

    def test_disjoint_from_legacy_uids(self):
        s = str(uuid.uuid4())
        legacy = {str(uuid.uuid5(RELAY_PHOTO_NS, "1001:42")),
                  str(uuid.uuid5(RELAY_PHOTO_NS, "1001:42:intrusion"))}
        assert series_intrusion_uid(s) not in legacy
        assert frame_media_uid(s, 1) not in legacy

    def test_namespace_unchanged(self):
        assert uuid.UUID("6f9b2d54-0d1e-4b8e-9c2a-7b3a1f0e5c11") == RELAY_PHOTO_NS


def test_frame_metadata_keys():
    series = _series()
    meta = frame_metadata(series, _photo(photo_id=2, photos_cnt=5))
    assert meta == {"relay_event_id": 42, "photo_id": 2, "photos_cnt": 5,
                    "series_uid": series.series_uid}
