from __future__ import annotations

from datetime import datetime
import json
from typing import TYPE_CHECKING, Any

from shared.database import AsyncPostgres
from shared.enums import RelayPhotoMetaKey

from ..models.media import LockerMedia, LockerMediaWithBytes, MediaKind

if TYPE_CHECKING:
    from uuid import UUID


_BASE_COLS = (
    "id, locker_id, sensor_id, security_event_id, kind, mime_type, "
    "size_bytes, width, height, duration_ms, captured_at, uploaded_at, "
    "uploaded_by, description, metadata"
)

_PHOTO_ID_KEY = RelayPhotoMetaKey.PHOTO_ID.value
_SERIES_UID_KEY = RelayPhotoMetaKey.SERIES_UID.value
_MEDIA_BY_EVENT_ORDER = (
    f"ORDER BY CASE WHEN jsonb_typeof(metadata -> '{_PHOTO_ID_KEY}') = 'number' "
    f"THEN (metadata ->> '{_PHOTO_ID_KEY}')::numeric END ASC NULLS LAST, "
    "captured_at ASC, id ASC"
)


def _row_to_media(row: dict[str, Any]) -> LockerMedia:
    return LockerMedia(
        id=row["id"],
        locker_id=row["locker_id"],
        sensor_id=row["sensor_id"],
        security_event_id=row["security_event_id"],
        kind=MediaKind(row["kind"]),
        mime_type=row["mime_type"],
        size_bytes=row["size_bytes"],
        width=row["width"],
        height=row["height"],
        duration_ms=row["duration_ms"],
        captured_at=row["captured_at"],
        uploaded_at=row["uploaded_at"],
        uploaded_by=row["uploaded_by"],
        description=row["description"],
        metadata=row["metadata"] or {},
    )


class MediaRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def insert(
        self,
        *,
        locker_id: UUID,
        sensor_id: UUID | None,
        security_event_id: int | None,
        kind: MediaKind,
        mime_type: str,
        bytes_: bytes,
        captured_at: datetime,
        width: int | None,
        height: int | None,
        duration_ms: int | None,
        description: str | None,
        metadata: dict[str, Any],
        uploaded_by: int | None = None,
        event_uid: str | None = None,
        security_event_uid: str | None = None,
    ) -> int | None:
        """Вставить медиа. Возвращает id, либо ``None`` если медиа с таким
        ``event_uid`` уже существует (ARC-02: дедуп повторной доставки)."""
        sql = (
            "INSERT INTO locker_media "
            "(locker_id, sensor_id, security_event_id, kind, mime_type, "
            "bytes, size_bytes, width, height, duration_ms, captured_at, "
            "uploaded_by, description, metadata, event_uid, security_event_uid) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (event_uid) DO NOTHING "
            "RETURNING id"
        )
        params = (
            str(locker_id),
            str(sensor_id) if sensor_id is not None else None,
            security_event_id,
            kind.value,
            mime_type,
            bytes_,
            len(bytes_),
            width,
            height,
            duration_ms,
            captured_at,
            uploaded_by,
            description,
            json.dumps(metadata),
            event_uid,
            security_event_uid,
        )
        row = await self._db.fetch_one(sql, params, read_only=False)
        if row is None:
            # ON CONFLICT DO NOTHING → 0 строк = дубликат по event_uid.
            return None
        return int(row["id"])

    async def list_by_event(self, event_id: int) -> list[LockerMedia]:
        sql = (
            f"SELECT {_BASE_COLS} FROM locker_media "
            "WHERE security_event_id = %s AND deleted_at IS NULL "
            f"{_MEDIA_BY_EVENT_ORDER}"
        )
        rows = await self._db.fetch_all(sql, (event_id,))
        return [_row_to_media(r) for r in rows]

    async def list_paginated(
        self,
        *,
        locker_id: UUID | None,
        kind: MediaKind | None,
        from_dt: datetime | None,
        to_dt: datetime | None,
        security_event_id: int | None,
        limit: int,
        offset: int,
    ) -> tuple[list[LockerMedia], int]:
        clauses: list[str] = ["deleted_at IS NULL"]
        params: list[Any] = []
        if locker_id is not None:
            clauses.append("locker_id = %s")
            params.append(str(locker_id))
        if kind is not None:
            clauses.append("kind = %s")
            params.append(kind.value)
        if from_dt is not None:
            clauses.append("captured_at >= %s")
            params.append(from_dt)
        if to_dt is not None:
            clauses.append("captured_at <= %s")
            params.append(to_dt)
        if security_event_id is not None:
            clauses.append("security_event_id = %s")
            params.append(security_event_id)
        where = " AND ".join(clauses)
        count_sql = f"SELECT COUNT(*) AS c FROM locker_media WHERE {where}"
        count_row = await self._db.fetch_one(count_sql, tuple(params))
        total = int(count_row["c"]) if count_row else 0
        list_sql = (
            f"SELECT {_BASE_COLS} FROM locker_media WHERE {where} "
            "ORDER BY captured_at DESC, id DESC LIMIT %s OFFSET %s"
        )
        rows = await self._db.fetch_all(list_sql, (*params, limit, offset))
        return [_row_to_media(r) for r in rows], total

    async def get_by_id_with_bytes(
        self, media_id: int
    ) -> LockerMediaWithBytes | None:
        sql = (
            f"SELECT {_BASE_COLS}, bytes FROM locker_media "
            "WHERE id = %s AND deleted_at IS NULL"
        )
        row = await self._db.fetch_one(sql, (media_id,))
        if row is None:
            return None
        media = _row_to_media(row)
        return LockerMediaWithBytes(media=media, bytes_=bytes(row["bytes"]))

    async def find_event_in_window(
        self,
        locker_id: UUID,
        sensor_id: UUID,
        occurred_at: datetime,
    ) -> int | None:
        """Legacy-привязка: ближайший security_event ±30 с для данного sensor_id.

        Берётся ближайшее по времени событие. События relay-серий (с
        ``metadata.series_uid``) исключены: их кадры привязываются только по
        ``security_event_uid``, и окно не должно прилепить к ним чужое медиа.
        """
        sql = (
            "SELECT id FROM security_events "
            "WHERE locker_id = %s AND sensor_id = %s "
            "  AND occurred_at BETWEEN %s::timestamptz - INTERVAL '30 seconds' "
            "                      AND %s::timestamptz + INTERVAL '30 seconds' "
            "  AND NOT (COALESCE(metadata, '{}'::jsonb) ? %s) "
            "ORDER BY ABS(EXTRACT(EPOCH FROM occurred_at - %s::timestamptz)) "
            "LIMIT 1"
        )
        row = await self._db.fetch_one(
            sql,
            (
                str(locker_id),
                str(sensor_id),
                occurred_at,
                occurred_at,
                _SERIES_UID_KEY,
                occurred_at,
            ),
        )
        if row is None:
            return None
        return int(row["id"])

    async def find_event_id_by_uid(self, event_uid: str) -> int | None:
        row = await self._db.fetch_one(
            "SELECT id FROM security_events WHERE event_uid = %s", (event_uid,),
        )
        return int(row["id"]) if row is not None else None

    async def relink_media(self, media_id: int) -> None:
        """Привязать одно медиа к событию по его security_event_uid, если оно уже есть."""
        await self._db.execute(
            "UPDATE locker_media m SET security_event_id = se.id "
            "FROM security_events se "
            "WHERE m.id = %s AND m.security_event_id IS NULL "
            "AND se.event_uid = m.security_event_uid",
            (media_id,),
            idempotent=True,
        )

    async def relink_by_media_uid(self, media_event_uid: str) -> None:
        """То же, что relink_media, но по event_uid самого медиа (повторная доставка)."""
        await self._db.execute(
            "UPDATE locker_media m SET security_event_id = se.id "
            "FROM security_events se "
            "WHERE m.event_uid = %s AND m.security_event_id IS NULL "
            "AND se.event_uid = m.security_event_uid",
            (media_event_uid,),
            idempotent=True,
        )

    async def link_pending_to_event(self, event_uid: str) -> None:
        """Привязать к только что записанному событию медиа, пришедшие раньше него."""
        await self._db.execute(
            "UPDATE locker_media m SET security_event_id = se.id "
            "FROM security_events se "
            "WHERE se.event_uid = %s AND m.security_event_uid = se.event_uid "
            "AND m.security_event_id IS NULL AND m.deleted_at IS NULL",
            (event_uid,),
            idempotent=True,
        )
