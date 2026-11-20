from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from uuid import UUID


class MediaKind(str, Enum):
    PHOTO = "photo"


@dataclass(slots=True)
class LockerMedia:
    id: int
    locker_id: UUID
    sensor_id: UUID | None
    security_event_id: int | None
    kind: MediaKind
    mime_type: str
    size_bytes: int
    width: int | None
    height: int | None
    duration_ms: int | None
    captured_at: datetime
    uploaded_at: datetime
    uploaded_by: int | None
    description: str | None
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class LockerMediaWithBytes:
    media: LockerMedia
    bytes_: bytes
