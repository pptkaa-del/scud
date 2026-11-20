"""Integration: кадры relay-фото → outbox и реестр relay_photo_series на реальной БД."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json as _json
import os
from typing import Any
import uuid

from prometheus_client import REGISTRY
import psycopg2
import pytest

from app.locker_data.db.repositories.locker import LockerRepository
from app.locker_data.db.repositories.relay_photo_series import RelayPhotoSeriesRepository
from app.locker_data.db.repositories.sensor import SensorRepository
from app.mqtt.ingest import RelayIngest
from app.mqtt.photo_series import series_intrusion_uid
from app.services.history_producers.media_producer import MediaProducer
from app.services.history_producers.security_event_producer import producer_for_sensor_type
from app.services.history_producers.telemetry_producer import TelemetryProducer
from shared.database import AsyncPostgres, PostgresConfig
from shared.enums import RelayMessageOutcome
from shared.metrics import SERVICE_NAME

# ---------------------------------------------------------------------------
# DB helpers (mirror test_relay_ingest_integration.py)
# ---------------------------------------------------------------------------

def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud"),
    )


def _sync_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _cleanup_locker(locker_id: str) -> None:
    """Hard-delete locker; cascade removes sensors and commands."""
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture
def db() -> AsyncPostgres:
    """AsyncPostgres pool for real-DB tests; closed after each test."""
    pg = AsyncPostgres(_db_config())
    try:
        yield pg
    finally:
        pg.close()


# ---------------------------------------------------------------------------
# Seeded relay locker fixture
# ---------------------------------------------------------------------------

@dataclass
class _RelayLocker:
    id: str
    relay_id: int


@pytest.fixture
def seeded_relay_locker() -> _RelayLocker:
    """Seed an mqtt locker via sync psycopg2; hard-delete on teardown.

    Uses sync psycopg2 (not AsyncPostgres) so there is no asyncio.run() inside
    the fixture — the single asyncio.run() lives only in the test body.
    """
    suffix = uuid.uuid4().hex[:8]
    relay_id = 5700000 + int(suffix[:4], 16) % 90000
    locker_id: str | None = None

    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lockers (inventory_number, transport, relay_id)"
                " VALUES (%s, %s, %s) RETURNING id",
                (f"INT-PHOTO-{suffix}", "mqtt", relay_id),
            )
            locker_id = str(cur.fetchone()[0])
        conn.commit()
    finally:
        conn.close()

    yield _RelayLocker(id=locker_id, relay_id=relay_id)  # type: ignore[misc]

    if locker_id is not None:
        _cleanup_locker(locker_id)
        _cleanup_outbound(locker_id)


def _cleanup_outbound(locker_id: str) -> None:
    """Remove outbound_history_events rows belonging to this test's locker."""
    conn = _sync_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM outbound_history_events"
                " WHERE channel = 'media'"
                " AND payload -> 'form_fields' ->> 'locker_id' = %s",
                (locker_id,),
            )
            cur.execute(
                "DELETE FROM outbound_history_events"
                " WHERE channel = 'security_event'"
                " AND payload ->> 'locker_id' = %s",
                (locker_id,),
            )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Stubs (mirror test_relay_ingest_integration.py)
# ---------------------------------------------------------------------------

class _StubQueue:
    """Stand-in for CommandQueue: stores registered transports, no dispatch."""

    def __init__(self) -> None:
        self._registered: dict[str, object] = {}

    async def register_connection(self, locker_id: str, transport: object) -> None:
        self._registered[locker_id] = transport

    async def unregister_connection(self, locker_id: str,
                                    transport: object = None) -> None:
        self._registered.pop(locker_id, None)

    def is_connected(self, locker_id: str) -> bool:
        return locker_id in self._registered

    def get_transport(self, locker_id: str) -> object | None:
        return self._registered.get(locker_id)


class _StubResults:
    async def handle_rep(self, locker_id: str, cmd_id: int, result: object,
                         received_at: object = None) -> RelayMessageOutcome:
        return RelayMessageOutcome.PROCESSED


class _StubMqtt:
    is_connected = True

    async def publish(self, topic: str, payload: bytes,
                      qos: int = 0, retain: bool = False) -> None:
        pass


# ---------------------------------------------------------------------------
# Frame builder
# ---------------------------------------------------------------------------

def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def _photo_frame(device_id: int, event_id: int, photos_cnt: int,
                 photo_id: int, body: bytes) -> bytes:
    """Build a relay photos frame: device_id|event_id|photos_cnt|photo_id|size|body."""
    return (
        _u32(device_id) + _u32(event_id) + _u32(photos_cnt)
        + _u32(photo_id) + _u32(len(body)) + body
    )


# ---------------------------------------------------------------------------
# Ingest factory wired to the test DB (incl. MediaProducer)
# ---------------------------------------------------------------------------

def _build_real_ingest(db: AsyncPostgres, *, series_burst: float = 10.0) -> RelayIngest:
    """RelayIngest с реальными репозиториями, MediaProducer и реестром серий."""
    return RelayIngest(
        repo=LockerRepository(db),
        command_queue=_StubQueue(),
        command_results=_StubResults(),
        mqtt_client=_StubMqtt(),
        telemetry=TelemetryProducer(db),
        offline_producer=None,
        sensor_repo=SensorRepository(db),
        allocator=None,
        security_producer_factory=lambda code: producer_for_sensor_type(db, code),
        media_producer=MediaProducer(db),
        photo_series_repo=RelayPhotoSeriesRepository(db),
        photo_series_burst=series_burst,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _jpeg(tag: str) -> bytes:
    return b"\xff\xd8\xff\xe0" + tag.encode() + b"\xff\xd9" + b"\x00" * 128


def _payloads(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [r["payload"] if isinstance(r["payload"], dict) else _json.loads(r["payload"])
            for r in rows]


async def _media_rows(db: AsyncPostgres, locker_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT payload, created_at, next_attempt_at FROM outbound_history_events "
        "WHERE channel = 'media' AND locker_id = %s ORDER BY id",
        (locker_id,),
    )
    return list(rows)


async def _security_payloads(db: AsyncPostgres, locker_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT payload FROM outbound_history_events "
        "WHERE channel = 'security_event' AND locker_id = %s ORDER BY id",
        (locker_id,),
    )
    return _payloads(list(rows))


def _frames_metric(disposition: str) -> float:
    value = REGISTRY.get_sample_value(
        "scud_relay_photo_frames_total",
        {"service": SERVICE_NAME, "disposition": disposition},
    )
    return value or 0.0


async def _series_rows(db: AsyncPostgres, locker_id: str) -> list[dict[str, Any]]:
    rows = await db.fetch_all(
        "SELECT id, series_uid::text AS series_uid, frame_sha, intrusion_enqueued_at, "
        "superseded_at FROM relay_photo_series WHERE locker_id = %s ORDER BY id",
        (locker_id,),
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_frames_enqueue_one_media_row_each_and_one_intrusion(
        db: AsyncPostgres, seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db)
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        for pid in (1, 2, 3):
            await ingest.handle_message("photos", _photo_frame(rid, 7, 3, pid, _jpeg(f"p{pid}")))

        media = await _media_rows(db, lid)
        assert len(media) == 3
        payloads = _payloads(media)
        ffs = [p["form_fields"] for p in payloads]
        assert len({ff["event_uid"] for ff in ffs}) == 3
        assert [_json.loads(ff["metadata"])["photo_id"] for ff in ffs] == [1, 2, 3]
        sec_uids = {ff["security_event_uid"] for ff in ffs}
        assert len(sec_uids) == 1
        for row in media:
            assert abs((row["next_attempt_at"] - row["created_at"]).total_seconds()) < 2
        assert all(ff["kind"] == "photo" and ff["mime_type"] == "image/jpeg" for ff in ffs)

        events = await _security_payloads(db, lid)
        assert len(events) == 1
        assert events[0]["event_type"] == "intrusion"
        assert events[0]["event_uid"] in sec_uids
        series_uid = _json.loads(ffs[0]["metadata"])["series_uid"]
        assert events[0]["metadata"]["series_uid"] == series_uid

        series = await _series_rows(db, lid)
        assert len(series) == 1
        assert set(series[0]["frame_sha"]) == {"1", "2", "3"}
        assert series[0]["intrusion_enqueued_at"] is not None

        # (c) повтор кадра 2 — новых строк нет
        await ingest.handle_message("photos", _photo_frame(rid, 7, 3, 2, _jpeg("p2")))
        assert len(await _media_rows(db, lid)) == 3
        assert len(await _security_payloads(db, lid)) == 1

        # (d) «рестарт» + кадр 4 — та же серия, без второго intrusion
        restarted = _build_real_ingest(db)
        await restarted.handle_message("photos", _photo_frame(rid, 7, 3, 4, _jpeg("p4")))
        media = await _media_rows(db, lid)
        assert len(media) == 4
        assert _json.loads(_payloads(media)[-1]["form_fields"]["metadata"])["series_uid"] \
            == series_uid
        assert len(await _security_payloads(db, lid)) == 1

    asyncio.run(scenario())


def test_event_id_reuse_resets_series(db: AsyncPostgres,
                                      seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db)
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        await ingest.handle_message("photos", _photo_frame(rid, 9, 2, 1, _jpeg("old")))
        await ingest.handle_message("photos", _photo_frame(rid, 9, 2, 1, _jpeg("new")))

        series = await _series_rows(db, lid)
        assert len(series) == 2
        assert series[0]["superseded_at"] is not None
        assert series[1]["superseded_at"] is None
        events = await _security_payloads(db, lid)
        assert len(events) == 2
        assert events[0]["event_uid"] != events[1]["event_uid"]

    asyncio.run(scenario())


def test_stale_epoch_duplicate_resets_series(db: AsyncPostgres,
                                             seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db)
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        await ingest.handle_message("photos", _photo_frame(rid, 5, 1, 1, _jpeg("same")))
        await db.execute(
            "UPDATE relay_photo_series SET first_seen_at = now() - interval '2 days' "
            "WHERE locker_id = %s", (lid,))
        await ingest.handle_message("photos", _photo_frame(rid, 5, 1, 1, _jpeg("same")))

        series = await _series_rows(db, lid)
        assert len(series) == 2
        assert series[0]["superseded_at"] is not None
        assert len(await _security_payloads(db, lid)) == 2

    asyncio.run(scenario())


def test_locker_delete_cascades_series(db: AsyncPostgres,
                                       seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db)
        await ingest.handle_message(
            "photos", _photo_frame(seeded_relay_locker.relay_id, 3, 1, 1, _jpeg("a")))
        assert len(await _series_rows(db, seeded_relay_locker.id)) == 1

    asyncio.run(scenario())
    _cleanup_locker(seeded_relay_locker.id)

    async def after() -> int:
        return len(await _series_rows(db, seeded_relay_locker.id))

    assert asyncio.run(after()) == 0


def test_concurrent_first_frames_create_one_series(db: AsyncPostgres,
                                                   seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingests = [_build_real_ingest(db), _build_real_ingest(db)]
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        await asyncio.gather(
            ingests[0].handle_message("photos", _photo_frame(rid, 13, 2, 1, _jpeg("a"))),
            ingests[1].handle_message("photos", _photo_frame(rid, 13, 2, 2, _jpeg("b"))),
        )
        series = await _series_rows(db, lid)
        assert len([s for s in series if s["superseded_at"] is None]) == 1
        assert len(series) == 1
        events = await _security_payloads(db, lid)
        assert len(events) == 1
        assert len(await _media_rows(db, lid)) == 2

    asyncio.run(scenario())


def test_concurrent_frames_of_new_series_enqueue_one_intrusion_and_count_one_new_series(
        db: AsyncPostgres, seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db)
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        new_series_before = _frames_metric("new_series")
        new_frame_before = _frames_metric("new_frame")
        event_ids = (41, 42, 43)
        for event_id in event_ids:
            await asyncio.gather(
                ingest.handle_message(
                    "photos", _photo_frame(rid, event_id, 2, 1, _jpeg(f"{event_id}a"))),
                ingest.handle_message(
                    "photos", _photo_frame(rid, event_id, 2, 2, _jpeg(f"{event_id}b"))),
            )

        series = await _series_rows(db, lid)
        assert len(series) == len(event_ids)
        assert all(set(s["frame_sha"]) == {"1", "2"} for s in series)
        assert all(s["intrusion_enqueued_at"] is not None for s in series)
        events = await _security_payloads(db, lid)
        assert sorted(e["event_uid"] for e in events) == sorted(
            series_intrusion_uid(s["series_uid"]) for s in series)
        assert len(await _media_rows(db, lid)) == 2 * len(event_ids)
        assert _frames_metric("new_series") - new_series_before == len(event_ids)
        assert _frames_metric("new_frame") - new_frame_before == len(event_ids)

    asyncio.run(scenario())


def test_concurrent_frames_of_one_series_take_one_rate_limit_token(
        db: AsyncPostgres, seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db, series_burst=2.0)
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        await asyncio.gather(
            ingest.handle_message("photos", _photo_frame(rid, 51, 2, 1, _jpeg("a"))),
            ingest.handle_message("photos", _photo_frame(rid, 51, 2, 2, _jpeg("b"))),
        )
        await ingest.handle_message("photos", _photo_frame(rid, 52, 1, 1, _jpeg("c")))

        assert len(await _series_rows(db, lid)) == 2
        assert len(await _security_payloads(db, lid)) == 2

    asyncio.run(scenario())


def test_concurrent_frames_of_new_series_with_last_token_are_both_recorded(
        db: AsyncPostgres, seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db, series_burst=1.0)
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        new_series_before = _frames_metric("new_series")
        new_frame_before = _frames_metric("new_frame")
        await asyncio.gather(
            ingest.handle_message("photos", _photo_frame(rid, 71, 2, 1, _jpeg("a"))),
            ingest.handle_message("photos", _photo_frame(rid, 71, 2, 2, _jpeg("b"))),
        )

        series = await _series_rows(db, lid)
        assert len(series) == 1
        assert set(series[0]["frame_sha"]) == {"1", "2"}
        assert len(await _media_rows(db, lid)) == 2
        assert len(await _security_payloads(db, lid)) == 1
        assert _frames_metric("new_series") - new_series_before == 1
        assert _frames_metric("new_frame") - new_frame_before == 1

    asyncio.run(scenario())


def test_purge_removes_only_expired_series(db: AsyncPostgres,
                                           seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        ingest = _build_real_ingest(db)
        rid, lid = seeded_relay_locker.relay_id, seeded_relay_locker.id
        await ingest.handle_message("photos", _photo_frame(rid, 21, 1, 1, _jpeg("old")))
        await ingest.handle_message("photos", _photo_frame(rid, 22, 1, 1, _jpeg("new")))
        await db.execute(
            "UPDATE relay_photo_series SET last_frame_at = now() - interval '91 days' "
            "WHERE locker_id = %s AND event_id = 21", (lid,))
        await RelayPhotoSeriesRepository(db).purge_older_than(90)
        rows = await db.fetch_all(
            "SELECT event_id FROM relay_photo_series WHERE locker_id = %s", (lid,))
        assert [r["event_id"] for r in rows] == [22]

    asyncio.run(scenario())


def test_maintenance_emits_intrusion_for_stuck_series(
        db: AsyncPostgres, seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        repo = RelayPhotoSeriesRepository(db)
        lid, rid = seeded_relay_locker.id, seeded_relay_locker.relay_id
        series, _ = await repo.create(lid, rid, 31, str(uuid.uuid4()), 2, 86400.0)
        await db.execute(
            "UPDATE relay_photo_series SET first_seen_at = now() - interval '5 minutes' "
            "WHERE id = %s", (series.id,))
        await _build_real_ingest(db).run_photo_maintenance(retention_days=90)
        events = await _security_payloads(db, lid)
        assert {e["event_uid"] for e in events} == {series_intrusion_uid(series.series_uid)}
        assert events[0]["metadata"]["series_uid"] == series.series_uid
        rows = await _series_rows(db, lid)
        assert rows[0]["intrusion_enqueued_at"] is not None

    asyncio.run(scenario())


def test_concurrent_enqueue_intrusion_inserts_one_outbox_row(
        db: AsyncPostgres, seeded_relay_locker: _RelayLocker) -> None:
    async def scenario() -> None:
        repo = RelayPhotoSeriesRepository(db)
        lid, rid = seeded_relay_locker.id, seeded_relay_locker.relay_id
        series, created = await repo.create(lid, rid, 61, str(uuid.uuid4()), 5, 86400.0)
        assert created
        payload = {"locker_id": lid, "event_type": "intrusion", "severity": "high",
                   "event_uid": series_intrusion_uid(series.series_uid)}
        results = await asyncio.gather(*(
            repo.enqueue_intrusion(series.id, "security_event",
                                   "/internal/api/v1/events/security", payload)
            for _ in range(5)))
        assert sorted(results) == [False, False, False, False, True]
        assert await _security_payloads(db, lid) == [payload]
        assert (await _series_rows(db, lid))[0]["intrusion_enqueued_at"] is not None

    asyncio.run(scenario())
