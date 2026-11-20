"""Классификация кадров relay-фото по реестру серий. Чистые функции, без I/O.

Каждый кадр топика ``photos`` — самостоятельный JPEG. Серия — это
(locker, relay_id, event_id) в пределах эпохи ``RELAY_PHOTO_SERIES_EPOCH_S``:
плата может переиспользовать event_id после перезагрузки, поэтому кадр с
другим содержимым на уже занятом photo_id или кадр к серии старше эпохи
начинает новую серию (RESET), а не приклеивается к старому инциденту.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
import hashlib
from typing import Any
import uuid

from shared.enums import RelayPhotoMetaKey

from ..locker_data.db.models.relay_photo_series import RelayPhotoSeries
from .relay_codec import Photo

RELAY_PHOTO_NS = uuid.UUID("6f9b2d54-0d1e-4b8e-9c2a-7b3a1f0e5c11")
MAX_PHOTOS_PER_SERIES = 16
JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"
# Плата добивает кадр до границы своего буфера нулями или 0xFF.
_PADDING = b"\x00\xff"


class FrameDisposition(StrEnum):
    NEW_SERIES = "new_series"
    NEW_FRAME = "new_frame"
    DUPLICATE = "duplicate"
    RESET = "reset"


@dataclass(slots=True, frozen=True)
class FrameClassification:
    disposition: FrameDisposition
    series: RelayPhotoSeries | None


def frame_digest(body: bytes) -> str:
    """sha256 кадра без хвостового паддинга: пересылка того же JPEG — тот же digest."""
    end = body.rfind(JPEG_EOI)
    canon = body[: end + len(JPEG_EOI)] if end >= 0 else body.rstrip(_PADDING)
    return hashlib.sha256(canon, usedforsecurity=False).hexdigest()


def validate_frame(photo: Photo) -> None:
    if not 1 <= photo.photos_cnt <= MAX_PHOTOS_PER_SERIES:
        raise ValueError(f"photos_cnt {photo.photos_cnt} out of 1..{MAX_PHOTOS_PER_SERIES}")
    if not 1 <= photo.photo_id <= MAX_PHOTOS_PER_SERIES:
        raise ValueError(f"photo_id {photo.photo_id} out of 1..{MAX_PHOTOS_PER_SERIES}")
    if photo.photo[: len(JPEG_SOI)] != JPEG_SOI:
        raise ValueError("photo body does not start with JPEG SOI")


def classify_frame(
    rows: Sequence[RelayPhotoSeries], photo_id: int, digest: str,
) -> FrameClassification:
    """Отнести кадр к серии. ``rows`` — все серии (locker, relay, event_id), новые первыми."""
    key = str(photo_id)
    for row in rows:
        if row.fresh and row.frame_sha.get(key) == digest:
            return FrameClassification(FrameDisposition.DUPLICATE, row)
    current = next((r for r in rows if r.superseded_at is None), None)
    if current is None:
        return FrameClassification(FrameDisposition.NEW_SERIES, None)
    if current.fresh and key not in current.frame_sha:
        return FrameClassification(FrameDisposition.NEW_FRAME, current)
    return FrameClassification(FrameDisposition.RESET, current)


def series_intrusion_uid(series_uid: str) -> str:
    return str(uuid.uuid5(RELAY_PHOTO_NS, f"series:{series_uid}:intrusion"))


def frame_media_uid(series_uid: str, photo_id: int) -> str:
    return str(uuid.uuid5(RELAY_PHOTO_NS, f"series:{series_uid}:photo:{photo_id}"))


def frame_metadata(series: RelayPhotoSeries, photo: Photo) -> dict[str, Any]:
    return {
        RelayPhotoMetaKey.RELAY_EVENT_ID.value: series.event_id,
        RelayPhotoMetaKey.PHOTO_ID.value: photo.photo_id,
        RelayPhotoMetaKey.PHOTOS_CNT.value: photo.photos_cnt,
        RelayPhotoMetaKey.SERIES_UID.value: series.series_uid,
    }
