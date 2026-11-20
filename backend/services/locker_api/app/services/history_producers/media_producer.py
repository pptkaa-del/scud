"""Продьюсер медиа-файлов (фото и видео) шкафов в history_api.

Собирает payload для channel='media' и ставит в очередь outbound_history_events.
Dispatcher собирает multipart/form-data из payload и шлёт
POST /internal/api/v1/media.
"""

from __future__ import annotations

import base64
import json
from typing import Any
import uuid

from app.services.history_producers._base import BaseHistoryProducer
from app.services.media_buffer import MediaTransferResult


class MediaProducer(BaseHistoryProducer):
    """Публикует медиа-файл в очередь outbound_history_events (channel='media').

    Dispatcher собирает multipart из payload и шлёт POST /internal/api/v1/media.
    """

    CHANNEL = "media"
    ENDPOINT = "/internal/api/v1/media"

    async def publish(
        self,
        *,
        result: MediaTransferResult,
        locker_id: str,
        security_event_id: int | None,
        event_uid: str | None = None,
    ) -> None:
        meta = result.meta
        form_fields: dict[str, Any] = {
            "locker_id": locker_id,
            "sensor_id": meta.sensor_id or "",
            "security_event_id": (
                str(security_event_id) if security_event_id is not None else ""
            ),
            "kind": meta.kind,
            "mime_type": meta.mime_type,
            "captured_at": meta.captured_at,
            "width": str(meta.width) if meta.width is not None else "",
            "height": str(meta.height) if meta.height is not None else "",
            "duration_ms": (
                str(meta.duration_ms) if meta.duration_ms is not None else ""
            ),
            "metadata": json.dumps(meta.metadata),
            # ARC-02: стабильный event_uid — повторная доставка дедуплицируется в history_api.
            "event_uid": event_uid if event_uid is not None else str(uuid.uuid4()),
        }
        if meta.security_event_uid is not None:
            form_fields["security_event_uid"] = meta.security_event_uid

        payload: dict[str, Any] = {
            "form_fields": form_fields,
            "file_bytes_b64": base64.b64encode(result.bytes_).decode("ascii"),
            "file_name": f"media-{meta.transfer_id}",
            "file_mime": meta.mime_type,
        }
        await self.enqueue(payload)
