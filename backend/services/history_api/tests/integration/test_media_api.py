"""Интеграционные тесты для Media API."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import json
import time
from uuid import uuid4

import psycopg2

from .conftest import (
    DB_CONFIG,
    generate_unique_locker_id,
    generate_unique_sensor_id,
    post_internal_media,
    post_internal_security_event,
)


def _create_security_event(locker_id: str, sensor_id: str | None = None) -> int:
    """Создать security_event напрямую через psycopg2 и вернуть его id."""
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO security_events (locker_id, sensor_id, event_type, severity)
                VALUES (%s, %s, %s, %s)
                RETURNING id
                """,
                (locker_id, sensor_id, "intrusion", "high"),
            )
            event_id: int = cur.fetchone()[0]
        conn.commit()
        return event_id
    finally:
        conn.close()


def test_internal_upload_then_list_by_event(api_client):
    """POST /internal/api/v1/media → GET /events/security/{id}/media → GET /media/{id}."""
    locker_id = generate_unique_locker_id()
    event_id = _create_security_event(locker_id)

    files = {"file": ("snap.jpg", b"\xff\xd8\xff\xe0fakeimg", "image/jpeg")}
    data = {
        "locker_id": locker_id,
        "kind": "photo",
        "mime_type": "image/jpeg",
        "captured_at": datetime.now(UTC).isoformat(),
        "security_event_id": str(event_id),
        "metadata": "{}",
    }
    resp = post_internal_media(files=files, data=data)
    assert resp.status_code == 201, resp.text
    media_id = resp.json()["id"]

    resp = api_client.get(f"/api/v1/events/security/{event_id}/media")
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert any(it["id"] == media_id for it in items)

    resp = api_client.get(f"/api/v1/media/{media_id}")
    assert resp.status_code == 200, resp.text
    assert resp.headers["Content-Type"] == "image/jpeg"
    assert resp.content == b"\xff\xd8\xff\xe0fakeimg"
    assert resp.headers["Content-Disposition"] == f'inline; filename="media-{media_id}.jpg"'


def test_media_list_paginated(api_client):
    """GET /api/v1/media возвращает пагинированный список."""
    locker_id = generate_unique_locker_id()
    for i in range(3):
        files = {"file": (f"img{i}.jpg", b"\xff\xd8\xff\xe0" + bytes([i]), "image/jpeg")}
        data = {
            "locker_id": locker_id,
            "kind": "photo",
            "mime_type": "image/jpeg",
            "captured_at": datetime.now(UTC).isoformat(),
            "metadata": "{}",
        }
        resp = post_internal_media(files=files, data=data)
        assert resp.status_code == 201

    resp = api_client.get(f"/api/v1/media?locker_id={locker_id}&limit=2")
    assert resp.status_code == 200
    body = resp.json()
    assert "items" in body
    assert "total" in body
    assert body["limit"] == 2
    assert len(body["items"]) <= 2


def test_media_not_found(api_client):
    """GET /api/v1/media/99999999 → 404."""
    resp = api_client.get("/api/v1/media/99999999")
    assert resp.status_code == 404


def test_internal_upload_event_window_match(api_client):
    """Upload без security_event_id но с event_sensor_id+event_occurred_at матчит событие."""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()
    occurred_at = datetime.now(UTC)

    event_id = _create_security_event(locker_id, sensor_id)

    files = {"file": ("snap2.jpg", b"\xff\xd8\xff\xe0test", "image/jpeg")}
    data = {
        "locker_id": locker_id,
        "kind": "photo",
        "mime_type": "image/jpeg",
        "captured_at": occurred_at.isoformat(),
        "event_sensor_id": sensor_id,
        "event_occurred_at": occurred_at.isoformat(),
        "metadata": "{}",
    }
    resp = post_internal_media(files=files, data=data)
    assert resp.status_code == 201, resp.text
    media_id = resp.json()["id"]

    resp = api_client.get(f"/api/v1/events/security/{event_id}/media")
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert any(it["id"] == media_id for it in items), (
        f"media_id={media_id} not found in event {event_id} items: {items}"
    )


# =============================================================================
# Relay-фото: один кадр — одна строка, привязка по security_event_uid
# =============================================================================


def _upload_frame(
    locker_id: str,
    *,
    captured_at: datetime,
    metadata: dict[str, object],
    security_event_id: int | None = None,
    security_event_uid: str | None = None,
    event_uid: str | None = None,
    event_sensor_id: str | None = None,
) -> int | None:
    data = {
        "locker_id": locker_id,
        "kind": "photo",
        "mime_type": "image/jpeg",
        "captured_at": captured_at.isoformat(),
        "metadata": json.dumps(metadata),
        "event_uid": event_uid or str(uuid4()),
    }
    if security_event_id is not None:
        data["security_event_id"] = str(security_event_id)
    if security_event_uid is not None:
        data["security_event_uid"] = security_event_uid
    if event_sensor_id is not None:
        data["event_sensor_id"] = event_sensor_id
        data["event_occurred_at"] = captured_at.isoformat()
    files = {"file": ("f.jpg", b"\xff\xd8\xff\xe0" + uuid4().bytes, "image/jpeg")}
    resp = post_internal_media(files=files, data=data)
    assert resp.status_code in (200, 201), resp.text
    media_id: int | None = resp.json().get("id")
    return media_id


def _post_event(
    locker_id: str, event_uid: str, sensor_id: str | None = None,
    metadata: dict[str, object] | None = None,
) -> int:
    payload: dict[str, object] = {
        "locker_id": locker_id,
        "event_type": "intrusion",
        "severity": "high",
        "event_uid": event_uid,
    }
    if sensor_id is not None:
        payload["sensor_id"] = sensor_id
    if metadata is not None:
        payload["metadata"] = metadata
    resp = post_internal_security_event(payload)
    assert resp.status_code in (200, 201), resp.text
    return _event_id_by_uid(event_uid)


def _event_id_by_uid(event_uid: str) -> int:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM security_events WHERE event_uid = %s", (event_uid,))
            row = cur.fetchone()
            assert row is not None
            return int(row[0])
    finally:
        conn.close()


def _linked_event(media_id: int) -> int | None:
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT security_event_id FROM locker_media WHERE id = %s", (media_id,))
            row = cur.fetchone()
            assert row is not None
            return row[0]
    finally:
        conn.close()


def _frame_meta(photo_id: int, series_uid: str) -> dict[str, object]:
    return {"relay_event_id": 42, "photo_id": photo_id, "photos_cnt": 3,
            "series_uid": series_uid}


def test_media_by_event_ordered_by_photo_id_legacy_last(api_client):
    locker_id = generate_unique_locker_id()
    event_id = _create_security_event(locker_id)
    series = str(uuid4())
    base = datetime.now(UTC)
    p3 = _upload_frame(locker_id, captured_at=base + timedelta(seconds=5),
                       metadata=_frame_meta(3, series), security_event_id=event_id)
    p1 = _upload_frame(locker_id, captured_at=base + timedelta(seconds=9),
                       metadata=_frame_meta(1, series), security_event_id=event_id)
    p2 = _upload_frame(locker_id, captured_at=base + timedelta(seconds=2),
                       metadata=_frame_meta(2, series), security_event_id=event_id)
    legacy = _upload_frame(locker_id, captured_at=base - timedelta(seconds=60),
                           metadata={}, security_event_id=event_id)

    resp = api_client.get(f"/api/v1/events/security/{event_id}/media")
    assert resp.status_code == 200, resp.text
    assert [it["id"] for it in resp.json()["items"]] == [p1, p2, p3, legacy]


def test_frame_before_event_is_backfilled():
    locker_id = generate_unique_locker_id()
    ev_uid = str(uuid4())
    media_id = _upload_frame(locker_id, captured_at=datetime.now(UTC),
                             metadata=_frame_meta(1, str(uuid4())),
                             security_event_uid=ev_uid)
    assert media_id is not None
    assert _linked_event(media_id) is None
    event_id = _post_event(locker_id, ev_uid)
    assert _linked_event(media_id) == event_id


def test_frame_after_event_links_immediately():
    locker_id = generate_unique_locker_id()
    ev_uid = str(uuid4())
    event_id = _post_event(locker_id, ev_uid)
    media_id = _upload_frame(locker_id, captured_at=datetime.now(UTC),
                             metadata=_frame_meta(1, str(uuid4())),
                             security_event_uid=ev_uid)
    assert media_id is not None
    assert _linked_event(media_id) == event_id


def test_uid_frame_never_window_matches_older_event():
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()
    old_event = _create_security_event(locker_id, sensor_id)
    new_uid = str(uuid4())
    media_id = _upload_frame(locker_id, captured_at=datetime.now(UTC),
                             metadata=_frame_meta(1, str(uuid4())),
                             security_event_uid=new_uid, event_sensor_id=sensor_id)
    assert media_id is not None
    assert _linked_event(media_id) is None
    new_event = _post_event(locker_id, new_uid, sensor_id=sensor_id)
    assert new_event != old_event
    assert _linked_event(media_id) == new_event


def test_event_redelivery_keeps_link():
    locker_id = generate_unique_locker_id()
    ev_uid = str(uuid4())
    media_id = _upload_frame(locker_id, captured_at=datetime.now(UTC),
                             metadata=_frame_meta(1, str(uuid4())),
                             security_event_uid=ev_uid)
    assert media_id is not None
    event_id = _post_event(locker_id, ev_uid)
    resp = post_internal_security_event({
        "locker_id": locker_id, "event_type": "intrusion", "severity": "high",
        "event_uid": ev_uid,
    })
    assert resp.status_code == 200, resp.text
    assert resp.json().get("status") == "duplicate"
    assert _linked_event(media_id) == event_id


def test_frame_redelivery_relinks_by_media_uid():
    locker_id = generate_unique_locker_id()
    ev_uid = str(uuid4())
    media_uid = str(uuid4())
    meta = _frame_meta(1, str(uuid4()))
    media_id = _upload_frame(locker_id, captured_at=datetime.now(UTC), metadata=meta,
                             security_event_uid=ev_uid, event_uid=media_uid)
    assert media_id is not None
    conn = psycopg2.connect(**DB_CONFIG)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO security_events (locker_id, event_type, severity, event_uid) "
                "VALUES (%s, 'intrusion', 'high', %s) RETURNING id",
                (locker_id, ev_uid),
            )
            event_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()
    assert _linked_event(media_id) is None
    assert _upload_frame(locker_id, captured_at=datetime.now(UTC), metadata=meta,
                         security_event_uid=ev_uid, event_uid=media_uid) is None
    assert _linked_event(media_id) == event_id


def test_legacy_window_skips_series_events():
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()
    _post_event(locker_id, str(uuid4()), sensor_id=sensor_id,
                metadata={"series_uid": str(uuid4())})
    media_id = _upload_frame(locker_id, captured_at=datetime.now(UTC), metadata={},
                             event_sensor_id=sensor_id)
    assert media_id is not None
    assert _linked_event(media_id) is None


def test_concurrent_frame_and_event_always_link():
    async def _race() -> list[int]:
        media_ids: list[int] = []
        for _ in range(50):
            locker_id = generate_unique_locker_id()
            ev_uid = str(uuid4())
            media_id, _ = await asyncio.gather(
                asyncio.to_thread(
                    _upload_frame, locker_id, captured_at=datetime.now(UTC),
                    metadata=_frame_meta(1, str(uuid4())), security_event_uid=ev_uid,
                ),
                asyncio.to_thread(_post_event, locker_id, ev_uid),
            )
            assert media_id is not None
            media_ids.append(media_id)
        return media_ids

    media_ids = asyncio.run(_race())
    unlinked = [m for m in media_ids if _linked_event(m) is None]
    assert unlinked == []


# Заметно ниже statement_timeout history_api (5 с): ожидание блокировки
# падает здесь, а не проходит после ретрая.
_INCIDENT_BUDGET_S = 2.5


def test_duplicate_event_deliveries_with_concurrent_frames_link_every_frame():
    frames_per_series = 5

    async def _incident() -> tuple[str, list[int]]:
        locker_id = generate_unique_locker_id()
        ev_uid, series_uid = str(uuid4()), str(uuid4())
        captured_at = datetime.now(UTC)
        started = time.monotonic()
        results = await asyncio.gather(
            asyncio.to_thread(_post_event, locker_id, ev_uid),
            asyncio.to_thread(_post_event, locker_id, ev_uid),
            *(asyncio.to_thread(_upload_frame, locker_id, captured_at=captured_at,
                                metadata=_frame_meta(pid, series_uid),
                                security_event_uid=ev_uid)
              for pid in range(1, frames_per_series + 1)),
        )
        assert time.monotonic() - started < _INCIDENT_BUDGET_S
        media_ids = [m for m in results[2:] if m is not None]
        assert len(media_ids) == frames_per_series
        return ev_uid, media_ids

    async def _burst() -> list[tuple[str, list[int]]]:
        return [await _incident() for _ in range(10)]

    for ev_uid, media_ids in asyncio.run(_burst()):
        event_id = _event_id_by_uid(ev_uid)
        assert [_linked_event(m) for m in media_ids] == [event_id] * frames_per_series
