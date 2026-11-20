"""Реестр серий relay-фото (таблица relay_photo_series).

Корректность при конкурентных кадрах держится на БД, а не на локах процесса:
``ux_relay_photo_series_current`` допускает одну текущую серию на
(locker, relay, event_id), ``create`` при гонке перечитывает победителя и
сообщает, кто создал строку, ``record_frame``/``supersede`` — условные UPDATE,
а ``enqueue_intrusion`` ставит intrusion в outbox одной командой с захватом серии.
"""
from __future__ import annotations

import json
from typing import Any

from dacite import from_dict

from shared.database import AsyncPostgres

from ..models.relay_photo_series import RelayPhotoSeries

_COLS = (
    "id, locker_id::text AS locker_id, relay_id, event_id, "
    "series_uid::text AS series_uid, photos_cnt, frame_sha, total_bytes, "
    "first_seen_at, last_frame_at, intrusion_enqueued_at, superseded_at, "
    "(first_seen_at > now() - make_interval(secs => %s)) AS fresh"
)
_EVENT_HISTORY_LIMIT = 8


def _to_series(row: dict[str, Any]) -> RelayPhotoSeries:
    return from_dict(RelayPhotoSeries, row)


class RelayPhotoSeriesRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def list_for_event(
        self, locker_id: str, relay_id: int, event_id: int, epoch_s: float,
    ) -> list[RelayPhotoSeries]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLS} FROM relay_photo_series "
            "WHERE locker_id = %s AND relay_id = %s AND event_id = %s "
            "ORDER BY id DESC LIMIT %s",
            (epoch_s, locker_id, relay_id, event_id, _EVENT_HISTORY_LIMIT),
        )
        return [_to_series(r) for r in rows]

    async def create(
        self, locker_id: str, relay_id: int, event_id: int, series_uid: str,
        photos_cnt: int, epoch_s: float,
    ) -> tuple[RelayPhotoSeries, bool]:
        """Создать текущую серию. Если её уже создал конкурент — его строка и False."""
        row = await self._db.fetch_one(
            "INSERT INTO relay_photo_series "
            "(locker_id, relay_id, event_id, series_uid, photos_cnt) "
            "VALUES (%s, %s, %s, %s, %s) "
            "ON CONFLICT (locker_id, relay_id, event_id) WHERE superseded_at IS NULL "
            f"DO NOTHING RETURNING {_COLS}",
            (locker_id, relay_id, event_id, series_uid, photos_cnt, epoch_s),
            read_only=False,
            idempotent=True,
        )
        if row is not None:
            return _to_series(row), True
        row = await self._db.fetch_one_strict(
            f"SELECT {_COLS} FROM relay_photo_series "
            "WHERE locker_id = %s AND relay_id = %s AND event_id = %s "
            "AND superseded_at IS NULL",
            (epoch_s, locker_id, relay_id, event_id),
            read_only=True,
        )
        return _to_series(row), False

    async def supersede(self, series_id: int) -> None:
        await self._db.execute(
            "UPDATE relay_photo_series SET superseded_at = now() "
            "WHERE id = %s AND superseded_at IS NULL",
            (series_id,),
            idempotent=True,
        )

    async def record_frame(
        self, series_id: int, photo_id: int, digest: str, size: int,
    ) -> None:
        key = str(photo_id)
        await self._db.execute(
            "UPDATE relay_photo_series SET "
            "frame_sha = frame_sha || jsonb_build_object(%s::text, %s::text), "
            "total_bytes = total_bytes + %s, last_frame_at = now() "
            "WHERE id = %s AND NOT frame_sha ? %s",
            (key, digest, size, series_id, key),
            idempotent=True,
        )

    async def enqueue_intrusion(
        self, series_id: int, channel: str, endpoint: str, payload: dict[str, Any],
    ) -> bool:
        """Поставить intrusion серии в outbox, если его ещё никто не поставил.

        Захват ``intrusion_enqueued_at`` и вставка в outbox — одна команда:
        конкурентный вызов ждёт блокировку строки серии и уже не проходит
        условие, а сбой не оставляет захват без события.

        Команда берёт строку серии, затем через FK — FOR KEY SHARE на lockers;
        физическое удаление локера блокирует в обратном порядке, и Postgres может
        оборвать одну из сторон deadlock'ом. Поэтому вызов не оборачивать в
        многошаговую транзакцию. Повтор после обрыва связи уже после COMMIT
        вернёт False, хотя событие поставлено: дедупу это не мешает, но
        security_events_total недосчитает одно событие.
        """
        row = await self._db.fetch_one(
            "WITH claim AS ("
            " UPDATE relay_photo_series SET intrusion_enqueued_at = now()"
            " WHERE id = %s AND intrusion_enqueued_at IS NULL"
            " RETURNING locker_id) "
            "INSERT INTO outbound_history_events (channel, endpoint, payload, locker_id) "
            "SELECT %s, %s, %s::jsonb, claim.locker_id FROM claim "
            "RETURNING id",
            (series_id, channel, endpoint, json.dumps(payload)),
            read_only=False,
            idempotent=True,
        )
        return row is not None

    async def list_pending_intrusion(
        self, older_than_s: float, limit: int, epoch_s: float,
    ) -> list[RelayPhotoSeries]:
        rows = await self._db.fetch_all(
            f"SELECT {_COLS} FROM relay_photo_series "
            "WHERE intrusion_enqueued_at IS NULL "
            "AND first_seen_at < now() - make_interval(secs => %s) "
            "ORDER BY first_seen_at LIMIT %s",
            (epoch_s, older_than_s, limit),
        )
        return [_to_series(r) for r in rows]

    async def purge_older_than(self, days: int) -> int:
        rows = await self._db.fetch_all(
            "DELETE FROM relay_photo_series "
            "WHERE last_frame_at < now() - make_interval(days => %s) RETURNING 1",
            (days,),
            read_only=False,
        )
        return len(rows)
