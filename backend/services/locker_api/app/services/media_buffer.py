"""Shared media-transfer dataclasses for the MQTT photo pipeline.

Describe one relay photo frame as it flows through app/mqtt/ingest.py into
MediaProducer and history_api.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class MediaTransferError(Exception):
    """Raised when a media transfer is invalid (seq gap, sha mismatch, etc.)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class MediaTransferMeta:
    transfer_id: str
    kind: str
    mime_type: str
    size_bytes: int
    captured_at: str
    sensor_id: str | None
    width: int | None
    height: int | None
    duration_ms: int | None
    # event_uid события безопасности: history_api привяжет медиа к нему,
    # даже если событие доедет позже медиа.
    security_event_uid: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class MediaTransferResult:
    meta: MediaTransferMeta
    bytes_: bytes
