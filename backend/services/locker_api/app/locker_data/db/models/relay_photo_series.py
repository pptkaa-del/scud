from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class RelayPhotoSeries:
    """Строка relay_photo_series; ``fresh`` вычисляет SQL относительно эпохи серии."""

    id: int
    locker_id: str
    relay_id: int
    event_id: int
    series_uid: str
    photos_cnt: int
    frame_sha: dict[str, str]
    total_bytes: int
    first_seen_at: datetime
    last_frame_at: datetime
    intrusion_enqueued_at: datetime | None
    superseded_at: datetime | None
    fresh: bool
