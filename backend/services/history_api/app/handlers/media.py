from __future__ import annotations

from collections.abc import Awaitable
from dataclasses import asdict
from datetime import UTC, datetime
import json
import logging
from typing import Any
from uuid import UUID

from app.handlers.base import BaseHandler
from app.handlers.internal_hmac import InternalHmacMixin
from app.history_data.db.models.media import LockerMedia, MediaKind
from app.history_data.db.repositories.media_repository import MediaRepository
from shared.auth import ProblemDetailsHandler
from shared.dtos import PaginatedResponse
from shared.http import ProblemTitle, ProblemType

logger = logging.getLogger(__name__)

ALLOWED_MIME_TYPES = {
    "image/jpeg",
    "image/png",
}
# Расширение файла по mime_type для Content-Disposition filename.
MIME_EXTENSIONS = {
    "image/jpeg": "jpg",
    "image/png": "png",
}
_MAX_MEDIA_MB = 10
_MAX_MEDIA_BYTES = _MAX_MEDIA_MB * 1024 * 1024


def _download_filename(media: LockerMedia) -> str:
    """Имя файла для Content-Disposition с расширением по mime_type."""
    base = f"media-{media.id}"
    ext = MIME_EXTENSIONS.get(media.mime_type)
    return f"{base}.{ext}" if ext else base


def _serialize(media: LockerMedia) -> dict[str, Any]:
    return {
        "id": media.id,
        "locker_id": str(media.locker_id),
        "sensor_id": str(media.sensor_id) if media.sensor_id else None,
        "security_event_id": media.security_event_id,
        "kind": media.kind.value,
        "mime_type": media.mime_type,
        "size_bytes": media.size_bytes,
        "width": media.width,
        "height": media.height,
        "duration_ms": media.duration_ms,
        "captured_at": media.captured_at.isoformat(),
        "uploaded_at": media.uploaded_at.isoformat(),
        "description": media.description,
        "metadata": media.metadata,
    }


class MediaListByEventHandler(BaseHandler):
    async def get(self, event_id: str) -> None:
        repo = MediaRepository(self.db)
        items = await repo.list_by_event(int(event_id))
        self.write_json({"items": [_serialize(m) for m in items]})


class MediaListHandler(BaseHandler):
    async def get(self) -> None:
        locker_id_raw = self.get_query_argument("locker_id", default=None)
        kind_raw = self.get_query_argument("kind", default=None)
        from_raw = self.get_query_argument("from", default=None)
        to_raw = self.get_query_argument("to", default=None)
        event_raw = self.get_query_argument("security_event_id", default=None)
        params = self.parse_pagination_params(default_limit=50, max_limit=500)
        if params is None:
            return
        limit, offset = params
        try:
            locker_id = UUID(locker_id_raw) if locker_id_raw else None
            kind = MediaKind(kind_raw) if kind_raw else None
            from_dt = datetime.fromisoformat(from_raw) if from_raw else None
            to_dt = datetime.fromisoformat(to_raw) if to_raw else None
            event_id = int(event_raw) if event_raw else None
        except (ValueError, KeyError):
            self.write_validation_error("invalid filter values")
            return
        repo = MediaRepository(self.db)
        items, total = await repo.list_paginated(
            locker_id=locker_id,
            kind=kind,
            from_dt=from_dt,
            to_dt=to_dt,
            security_event_id=event_id,
            limit=limit,
            offset=offset,
        )
        self.write_json(
            asdict(
                PaginatedResponse(
                    items=[_serialize(m) for m in items],
                    total=total,
                    limit=limit,
                    offset=offset,
                )
            )
        )


class MediaDetailHandler(BaseHandler):
    async def get(self, media_id: str) -> None:
        repo = MediaRepository(self.db)
        result = await repo.get_by_id_with_bytes(int(media_id))
        if result is None:
            self.write_not_found("Media not found")
            return
        self.set_header("Content-Type", result.media.mime_type)
        self.set_header("Content-Length", str(len(result.bytes_)))
        self.set_header(
            "Content-Disposition",
            f'inline; filename="{_download_filename(result.media)}"',
        )
        self.write(result.bytes_)


class InternalMediaUploadHandler(InternalHmacMixin, ProblemDetailsHandler):
    """multipart upload от locker_api dispatcher.

    Принимает form-fields: locker_id, sensor_id, security_event_id,
    security_event_uid, kind, mime_type, captured_at (ISO), width, height,
    duration_ms, description, metadata (json), event_sensor_id,
    event_occurred_at, event_uid. Файл — в files['file'][0].

    Привязка к событию, по приоритету:
    1. явный ``security_event_id``;
    2. ``security_event_uid`` — событие с этим event_uid. Если его ещё нет,
       медиа пишется без привязки и дотягивается позже: ``relink_media`` сразу
       после вставки и ``link_pending_to_event`` при записи события. Окно
       ±30 с в этом случае не используется;
    3. legacy: ``event_sensor_id`` + ``event_occurred_at`` — ближайшее событие
       ±30 с без ``metadata.series_uid``.

    Внутренний эндпоинт: закрыт снаружи через Apache <Location /internal>.
    HMAC-подпись (SEC-05): весь raw multipart-body подписывается ключом
    ``INTERNAL_HISTORY_HMAC_KEY`` (через ``body_sha256`` над сырым телом ДО
    парсинга form-data) — схема едина с JSON-эндпойнтами. ``event_uid`` (ARC-02)
    дедуплицирует повторную доставку (10 MB не дублируются); на дубликате
    привязка повторяется. Ошибка привязки → 500, outbox повторит доставку.
    """

    async def post(self) -> None:
        if not self.verify_internal_hmac():
            logger.warning(
                "internal/api/v1/media signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        files = self.request.files.get("file") or []
        if not files:
            self.write_problem(
                ProblemType.VALIDATION_ERROR,
                ProblemTitle.VALIDATION_ERROR,
                400,
                "file field required",
            )
            return
        body = files[0].body
        if len(body) > _MAX_MEDIA_BYTES:
            self.write_problem(
                ProblemType.PAYLOAD_TOO_LARGE,
                ProblemTitle.PAYLOAD_TOO_LARGE,
                413,
                f"media exceeds {_MAX_MEDIA_MB} MB",
            )
            return
        try:
            kind = MediaKind(self.get_body_argument("kind"))
            mime_type = self.get_body_argument("mime_type")
            if mime_type not in ALLOWED_MIME_TYPES:
                self.write_problem(
                    ProblemType.UNSUPPORTED_MEDIA_TYPE,
                    ProblemTitle.UNSUPPORTED_MEDIA_TYPE,
                    415,
                    f"mime {mime_type} not allowed",
                )
                return
            locker_id = UUID(self.get_body_argument("locker_id"))
            sensor_id_raw = self.get_body_argument("sensor_id", default="")
            sensor_id = UUID(sensor_id_raw) if sensor_id_raw else None
            event_raw = self.get_body_argument("security_event_id", default="")
            event_id: int | None = int(event_raw) if event_raw else None
            sec_uid_raw = self.get_body_argument("security_event_uid", default="")
            sec_uid = str(UUID(sec_uid_raw)) if sec_uid_raw else None
            captured_at_raw = self.get_body_argument("captured_at")
            if not captured_at_raw:
                raise ValueError("captured_at is required")
            captured_at = datetime.fromisoformat(captured_at_raw)
            if captured_at.tzinfo is None:
                captured_at = captured_at.replace(tzinfo=UTC)
            # COR-29: int() для размеров — внутри try, иначе нечисловой width
            # поднимал бы ValueError ВНЕ блока валидации → 500 вместо 400.
            width_raw = self.get_body_argument("width", default="")
            height_raw = self.get_body_argument("height", default="")
            duration_raw = self.get_body_argument("duration_ms", default="")
            width = int(width_raw) if width_raw else None
            height = int(height_raw) if height_raw else None
            duration_ms = int(duration_raw) if duration_raw else None
            metadata_raw = self.get_body_argument("metadata", default="{}")
            description = self.get_body_argument("description", default="") or None
            # ARC-02: продьюсер-генерируемый event_uid для идемпотентности.
            event_uid_raw = self.get_body_argument("event_uid", default="")
            event_uid = str(UUID(event_uid_raw)) if event_uid_raw else None
        except (ValueError, KeyError):
            self.write_problem(
                ProblemType.VALIDATION_ERROR,
                ProblemTitle.VALIDATION_ERROR,
                400,
                "invalid form fields",
            )
            return
        try:
            metadata: dict[str, Any] = json.loads(metadata_raw) if metadata_raw else {}
        except json.JSONDecodeError:
            self.write_problem(
                ProblemType.VALIDATION_ERROR,
                ProblemTitle.VALIDATION_ERROR,
                400,
                "metadata must be valid JSON object",
            )
            return

        repo = MediaRepository(self.db)
        if event_id is None and sec_uid is not None:
            event_id = await repo.find_event_id_by_uid(sec_uid)
        elif event_id is None:
            event_id = await self._legacy_window_match(repo, locker_id)

        media_id = await repo.insert(
            locker_id=locker_id,
            sensor_id=sensor_id,
            security_event_id=event_id,
            kind=kind,
            mime_type=mime_type,
            bytes_=body,
            captured_at=captured_at,
            width=width,
            height=height,
            duration_ms=duration_ms,
            description=description,
            metadata=metadata,
            event_uid=event_uid,
            security_event_uid=sec_uid,
        )
        if media_id is None:
            # Дубликат по event_uid — медиа уже сохранено предыдущей доставкой.
            # Идемпотентный 200, чтобы dispatcher не ретраил 10 MB бесконечно.
            logger.info(
                "Internal: media with event_uid=%s already exists (dedup)",
                event_uid,
            )
            if event_uid is not None and not await self._relink(
                repo.relink_by_media_uid(event_uid)
            ):
                return
            self.set_status(200)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"status": "duplicate"}))
            return
        if event_id is None and sec_uid is not None and not await self._relink(
            repo.relink_media(media_id)
        ):
            return
        self.set_status(201)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.write(json.dumps({"id": media_id}))

    async def _legacy_window_match(
        self, repo: MediaRepository, locker_id: UUID,
    ) -> int | None:
        event_sensor_id_raw = self.get_body_argument("event_sensor_id", default="")
        event_occurred_at_raw = self.get_body_argument("event_occurred_at", default="")
        if not (event_sensor_id_raw and event_occurred_at_raw):
            return None
        try:
            ev_sensor_id = UUID(event_sensor_id_raw)
            ev_occurred_at = datetime.fromisoformat(event_occurred_at_raw)
        except (ValueError, KeyError):
            return None
        if ev_occurred_at.tzinfo is None:
            ev_occurred_at = ev_occurred_at.replace(tzinfo=UTC)
        return await repo.find_event_in_window(
            locker_id=locker_id,
            sensor_id=ev_sensor_id,
            occurred_at=ev_occurred_at,
        )

    async def _relink(self, relink: Awaitable[None]) -> bool:
        try:
            await relink
        except Exception as exc:
            logger.error("Internal: media relink failed: %s", exc, exc_info=True)
            self.write_internal_error(str(exc))
            return False
        return True
