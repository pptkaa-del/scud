"""Unit: relay-фото — каждый кадр публикуется сразу, серия ведётся в реестре."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
import logging
import uuid

import pytest

from app.locker_data.db.models.relay_photo_series import RelayPhotoSeries
from app.mqtt import ingest as ingest_mod
from app.mqtt.ingest import RelayIngest
from app.mqtt.photo_series import frame_digest, frame_media_uid, series_intrusion_uid
from shared.enums import RelayMessageOutcome

_DAY = 86400.0


@dataclass
class _Row:
    id: int
    locker_id: str
    relay_id: int
    event_id: int
    series_uid: str
    photos_cnt: int
    first_seen: float
    frame_sha: dict[str, str] = field(default_factory=dict)
    total_bytes: int = 0
    last_frame: float = 0.0
    intrusion_enqueued: float | None = None
    superseded: float | None = None


def _ts(t: float) -> datetime:
    return datetime.fromtimestamp(t, UTC)


class FakeSeriesRepo:
    """In-memory relay_photo_series: unique-current, fresh по инжектируемым часам.

    Каждый вызов уступает цикл, как сетевой запрос к БД, поэтому параллельные
    кадры чередуются так же, как на реальной базе.
    """

    def __init__(self) -> None:
        self.now = 1_000_000.0
        self.rows: list[_Row] = []
        self.outbox: list[dict[str, object]] = []
        self.calls: list[str] = []
        self.purged_with: list[int] = []
        self.fail: set[str] = set()

    def _snap(self, r: _Row, epoch_s: float) -> RelayPhotoSeries:
        return RelayPhotoSeries(
            id=r.id, locker_id=r.locker_id, relay_id=r.relay_id, event_id=r.event_id,
            series_uid=r.series_uid, photos_cnt=r.photos_cnt, frame_sha=dict(r.frame_sha),
            total_bytes=r.total_bytes, first_seen_at=_ts(r.first_seen),
            last_frame_at=_ts(r.last_frame or r.first_seen),
            intrusion_enqueued_at=_ts(r.intrusion_enqueued) if r.intrusion_enqueued else None,
            superseded_at=_ts(r.superseded) if r.superseded else None,
            fresh=r.first_seen > self.now - epoch_s,
        )

    def _current(self, locker_id: str, relay_id: int, event_id: int) -> _Row | None:
        return next((r for r in self.rows if (r.locker_id, r.relay_id, r.event_id)
                     == (locker_id, relay_id, event_id) and r.superseded is None), None)

    async def _check(self, name: str) -> None:
        await asyncio.sleep(0)
        self.calls.append(name)
        if name in self.fail:
            raise RuntimeError(f"{name} failed")

    async def list_for_event(self, locker_id, relay_id, event_id, epoch_s):
        await self._check("list_for_event")
        rows = [r for r in self.rows
                if (r.locker_id, r.relay_id, r.event_id) == (locker_id, relay_id, event_id)]
        return [self._snap(r, epoch_s) for r in sorted(rows, key=lambda r: -r.id)][:8]

    async def create(self, locker_id, relay_id, event_id, series_uid, photos_cnt, epoch_s):
        await self._check("create")
        current = self._current(locker_id, relay_id, event_id)
        if current is not None:
            return self._snap(current, epoch_s), False
        current = _Row(id=len(self.rows) + 1, locker_id=locker_id, relay_id=relay_id,
                       event_id=event_id, series_uid=series_uid,
                       photos_cnt=photos_cnt, first_seen=self.now)
        self.rows.append(current)
        return self._snap(current, epoch_s), True

    async def supersede(self, series_id):
        await self._check("supersede")
        row = self.rows[series_id - 1]
        if row.superseded is None:
            row.superseded = self.now

    async def record_frame(self, series_id, photo_id, digest, size):
        await self._check("record_frame")
        row = self.rows[series_id - 1]
        if str(photo_id) not in row.frame_sha:
            row.frame_sha[str(photo_id)] = digest
            row.total_bytes += size
            row.last_frame = self.now

    async def enqueue_intrusion(self, series_id, channel, endpoint, payload):
        await self._check("enqueue_intrusion")
        row = self.rows[series_id - 1]
        if row.intrusion_enqueued is not None:
            return False
        row.intrusion_enqueued = self.now
        self.outbox.append(payload)
        return True

    async def list_pending_intrusion(self, older_than_s, limit, epoch_s):
        await self._check("list_pending_intrusion")
        rows = [r for r in self.rows
                if r.intrusion_enqueued is None and r.first_seen < self.now - older_than_s]
        return [self._snap(r, epoch_s) for r in rows[:limit]]

    async def purge_older_than(self, days):
        await self._check("purge_older_than")
        self.purged_with.append(days)
        return 0

    def current_rows(self) -> list[_Row]:
        return [r for r in self.rows if r.superseded is None]


class FakeLockers:
    async def get_locker_id_by_relay_id(self, relay_id):
        await asyncio.sleep(0)
        return {1001: "L1"}.get(relay_id)


class FakeMedia:
    def __init__(self) -> None:
        self.published: list[tuple[str, object, int | None, str | None]] = []
        self.fail = False

    async def publish(self, *, result, locker_id, security_event_id, event_uid=None):
        if self.fail:
            raise RuntimeError("outbox down")
        self.published.append((locker_id, result, security_event_id, event_uid))


class FakeSecurity:
    CHANNEL = "security_event"
    ENDPOINT = "/internal/api/v1/events/security"

    def __init__(self) -> None:
        self.enqueued = 0

    def __call__(self, code):
        return self

    def build_payload(self, locker_id, sensor_id, violations, *, sensor_type_code=None,
                      raw_value=None, event_uid=None, extra_metadata=None):
        return {"locker_id": locker_id, "code": sensor_type_code,
                "event_uid": event_uid, "extra": extra_metadata}

    def record_enqueued(self):
        self.enqueued += 1


class _Env:
    def __init__(self, *, max_bytes: int = 10 * 1024 * 1024, burst: float = 10.0) -> None:
        self.repo = FakeSeriesRepo()
        self.media = FakeMedia()
        self.security = FakeSecurity()
        self.max_bytes = max_bytes
        self.burst = burst
        self.ingest = self.new_ingest()

    @property
    def intrusions(self) -> list[dict[str, object]]:
        return self.repo.outbox

    def new_ingest(self) -> RelayIngest:
        return RelayIngest(
            repo=FakeLockers(), command_queue=None, command_results=None, mqtt_client=None,
            security_producer_factory=self.security, media_producer=self.media,
            photo_series_repo=self.repo, photo_series_epoch_s=_DAY,
            photo_series_max_bytes=self.max_bytes, photo_series_rate=0.1,
            photo_series_burst=self.burst,
        )


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def _jpeg(tag: str, pad: int = 0) -> bytes:
    return b"\xff\xd8\xff\xe0" + tag.encode() + b"\xff\xd9" + b"\x00" * pad


def _frame(event_id: int, photo_id: int, body: bytes, photos_cnt: int = 3,
           device_id: int = 1001) -> bytes:
    return (_u32(device_id) + _u32(event_id) + _u32(photos_cnt) + _u32(photo_id)
            + _u32(len(body)) + body)


@pytest.fixture
def frames(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(ingest_mod, "record_relay_photo_frame", seen.append)
    return seen


@pytest.fixture
def outcomes(monkeypatch):
    seen: list[RelayMessageOutcome] = []
    monkeypatch.setattr(ingest_mod, "record_relay_message", seen.append)
    return seen


def _run(coro):
    return asyncio.run(coro)


def test_three_frames_publish_three_media_and_one_intrusion(outcomes):
    env = _Env()

    async def scenario():
        for pid in (1, 2, 3):
            await env.ingest.handle_message("photos", _frame(42, pid, _jpeg(f"p{pid}", 64)))

    _run(scenario())
    assert len(env.intrusions) == 1
    assert len(env.media.published) == 3
    series_uid = env.repo.rows[0].series_uid
    intrusion_uid = series_intrusion_uid(series_uid)
    ev = env.intrusions[0]
    assert ev["code"] == "intrusion"
    assert ev["event_uid"] == intrusion_uid
    assert ev["extra"] == {"relay_event_id": 42, "series_uid": series_uid, "photos_cnt": 3}
    uids = [m[3] for m in env.media.published]
    assert uids == [frame_media_uid(series_uid, p) for p in (1, 2, 3)]
    assert len({m[1].meta.transfer_id for m in env.media.published}) == 3
    for pid, (locker_id, result, sec_id, _uid) in enumerate(env.media.published, start=1):
        assert locker_id == "L1"
        assert sec_id is None
        assert result.meta.security_event_uid == intrusion_uid
        assert result.meta.metadata == {"relay_event_id": 42, "photo_id": pid,
                                        "photos_cnt": 3, "series_uid": series_uid}
        assert result.bytes_ == _jpeg(f"p{pid}", 64)
    assert outcomes == [RelayMessageOutcome.PROCESSED] * 3
    assert set(env.repo.rows[0].frame_sha) == {"1", "2", "3"}


def test_first_frame_publishes_immediately():
    env = _Env()
    _run(env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"), photos_cnt=5)))
    assert len(env.media.published) == 1
    assert len(env.intrusions) == 1


def test_out_of_order_frames_emit_intrusion_once_on_first_arrival():
    env = _Env()

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 3, _jpeg("c")))
        assert len(env.intrusions) == 1
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a")))
        await env.ingest.handle_message("photos", _frame(42, 2, _jpeg("b")))

    _run(scenario())
    assert len(env.intrusions) == 1
    assert [m[1].meta.metadata["photo_id"] for m in env.media.published] == [3, 1, 2]


def test_duplicate_after_complete_series_publishes_nothing(outcomes):
    env = _Env()

    async def scenario():
        for pid in (1, 2, 3):
            await env.ingest.handle_message("photos", _frame(42, pid, _jpeg(f"p{pid}")))
        await env.ingest.handle_message("photos", _frame(42, 2, _jpeg("p2", 300)))

    _run(scenario())
    assert len(env.media.published) == 3
    assert len(env.intrusions) == 1
    assert outcomes[-1] is RelayMessageOutcome.PROCESSED


def test_event_id_reuse_with_new_bytes_resets_series(caplog):
    env = _Env()

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("old")))
        with caplog.at_level(logging.WARNING, logger="app.mqtt.ingest"):
            await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("new")))

    _run(scenario())
    assert len(env.repo.rows) == 2
    assert env.repo.rows[0].superseded is not None
    assert env.repo.rows[1].superseded is None
    uids = [e["event_uid"] for e in env.intrusions]
    assert len(uids) == 2
    assert uids[0] != uids[1]
    assert any("reset" in r.getMessage() for r in caplog.records)


def test_partial_series_frames_published_and_photos_cnt_mismatch_warned(caplog):
    env = _Env()

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"), photos_cnt=5))
        await env.ingest.handle_message("photos", _frame(42, 2, _jpeg("b"), photos_cnt=5))
        with caplog.at_level(logging.WARNING, logger="app.mqtt.ingest"):
            await env.ingest.handle_message("photos", _frame(42, 3, _jpeg("c"), photos_cnt=3))

    _run(scenario())
    assert len(env.media.published) == 3
    assert any("photos_cnt" in r.getMessage() for r in caplog.records)


def test_photo_id_beyond_photos_cnt_is_published_with_warning(caplog):
    env = _Env()
    with caplog.at_level(logging.WARNING, logger="app.mqtt.ingest"):
        _run(env.ingest.handle_message("photos", _frame(42, 4, _jpeg("d"), photos_cnt=3)))
    assert len(env.media.published) == 1
    assert any("photo_id" in r.getMessage() for r in caplog.records)


def test_intrusion_failure_is_retried_on_duplicate():
    env = _Env()
    env.repo.fail.add("enqueue_intrusion")

    async def scenario():
        for pid in (1, 2):
            await env.ingest.handle_message("photos", _frame(42, pid, _jpeg(f"p{pid}")))
        env.repo.fail.discard("enqueue_intrusion")
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("p1")))
        await env.ingest.handle_message("photos", _frame(42, 2, _jpeg("p2")))

    _run(scenario())
    assert len(env.intrusions) == 1
    assert env.repo.rows[0].intrusion_enqueued is not None
    assert len(env.media.published) == 2


def test_media_failure_keeps_frame_unrecorded_and_retry_republishes(outcomes):
    env = _Env()
    env.media.fail = True

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a")))
        assert "record_frame" not in env.repo.calls
        env.media.fail = False
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a")))

    _run(scenario())
    assert outcomes == [RelayMessageOutcome.HANDLER_ERROR, RelayMessageOutcome.PROCESSED]
    assert len(env.media.published) == 1
    assert env.media.published[0][3] == frame_media_uid(env.repo.rows[0].series_uid, 1)
    assert env.repo.rows[0].frame_sha == {"1": frame_digest(_jpeg("a"))}


def test_series_byte_cap_drops_frame(outcomes):
    body = _jpeg("x" * 40)
    env = _Env(max_bytes=len(body) + 10)

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 1, body))
        await env.ingest.handle_message("photos", _frame(42, 2, _jpeg("y" * 40)))

    _run(scenario())
    assert outcomes == [RelayMessageOutcome.PROCESSED, RelayMessageOutcome.DROPPED_MALFORMED]
    assert len(env.media.published) == 1


def test_frame_without_soi_is_malformed(outcomes):
    env = _Env()
    _run(env.ingest.handle_message("photos", _frame(42, 1, b"\x00\x00JPEGISH")))
    assert outcomes == [RelayMessageOutcome.DROPPED_MALFORMED]
    assert env.repo.calls == []


def test_restart_continues_series_without_new_intrusion():
    env = _Env()

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"), photos_cnt=4))
        restarted = env.new_ingest()
        await restarted.handle_message("photos", _frame(42, 4, _jpeg("d"), photos_cnt=4))

    _run(scenario())
    assert len(env.repo.rows) == 1
    assert len(env.intrusions) == 1
    series_uid = env.repo.rows[0].series_uid
    assert env.media.published[1][3] == frame_media_uid(series_uid, 4)


def test_new_series_rate_limited_per_relay(outcomes):
    env = _Env(burst=2.0)

    async def scenario():
        for ev in (1, 2, 3):
            await env.ingest.handle_message("photos", _frame(ev, 1, _jpeg(f"e{ev}")))
        await env.ingest.handle_message("photos", _frame(1, 2, _jpeg("e1b")))

    _run(scenario())
    assert outcomes == [RelayMessageOutcome.PROCESSED, RelayMessageOutcome.PROCESSED,
                        RelayMessageOutcome.DROPPED_RATE_LIMITED, RelayMessageOutcome.PROCESSED]
    assert len(env.repo.rows) == 2


def test_concurrent_first_frames_share_one_series():
    env = _Env()

    async def scenario():
        await asyncio.gather(
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))),
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))),
        )

    _run(scenario())
    assert len(env.repo.current_rows()) == 1
    assert len(env.repo.rows) == 1
    assert [e["event_uid"] for e in env.intrusions] == \
        [series_intrusion_uid(env.repo.rows[0].series_uid)]


def test_concurrent_distinct_first_frames_share_one_series():
    env = _Env()

    async def scenario():
        await asyncio.gather(
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))),
            env.ingest.handle_message("photos", _frame(42, 2, _jpeg("b"))),
        )

    _run(scenario())
    assert len(env.repo.rows) == 1
    assert set(env.repo.rows[0].frame_sha) == {"1", "2"}
    assert [e["event_uid"] for e in env.intrusions] == \
        [series_intrusion_uid(env.repo.rows[0].series_uid)]
    assert len(env.media.published) == 2


def test_concurrent_frames_of_new_series_enqueue_one_intrusion_and_one_new_series(frames):
    env = _Env()

    async def scenario():
        await asyncio.gather(
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))),
            env.ingest.handle_message("photos", _frame(42, 2, _jpeg("b"))),
        )

    _run(scenario())
    assert len(env.intrusions) == 1
    assert env.security.enqueued == 1
    assert sorted(frames) == ["new_frame", "new_series"]
    assert set(env.repo.rows[0].frame_sha) == {"1", "2"}


def test_concurrent_redelivery_of_first_frame_counts_one_new_series(frames):
    env = _Env()

    async def scenario():
        await asyncio.gather(
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))),
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a", 32))),
        )

    _run(scenario())
    assert len(env.intrusions) == 1
    assert len(env.repo.rows) == 1
    assert frames.count("new_series") == 1
    assert {m[3] for m in env.media.published} == {
        frame_media_uid(env.repo.rows[0].series_uid, 1)}


def test_concurrent_frames_of_one_series_take_one_rate_limit_token(outcomes):
    env = _Env(burst=2.0)

    async def scenario():
        await asyncio.gather(
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))),
            env.ingest.handle_message("photos", _frame(42, 2, _jpeg("b"))),
        )
        await env.ingest.handle_message("photos", _frame(43, 1, _jpeg("c")))

    _run(scenario())
    assert outcomes == [RelayMessageOutcome.PROCESSED] * 3
    assert len(env.repo.rows) == 2


def test_concurrent_new_series_frames_with_last_token_are_both_recorded(outcomes, frames):
    env = _Env(burst=1.0)

    async def scenario():
        await asyncio.gather(
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))),
            env.ingest.handle_message("photos", _frame(42, 2, _jpeg("b"))),
        )

    _run(scenario())
    assert outcomes == [RelayMessageOutcome.PROCESSED] * 2
    assert sorted(frames) == ["new_frame", "new_series"]
    assert len(env.repo.rows) == 1
    assert set(env.repo.rows[0].frame_sha) == {"1", "2"}
    assert len(env.media.published) == 2
    assert len(env.ingest._series_claims) == 0


def test_concurrent_frames_on_two_ingests_resolve_by_create(outcomes, frames):
    env = _Env(burst=2.0)
    ingests = [env.ingest, env.new_ingest()]

    async def scenario():
        await asyncio.gather(
            ingests[0].handle_message("photos", _frame(42, 1, _jpeg("a"))),
            ingests[1].handle_message("photos", _frame(42, 2, _jpeg("b"))),
        )
        for ingest, events in zip(ingests, ((43, 44), (45, 46)), strict=True):
            for ev in events:
                await ingest.handle_message("photos", _frame(ev, 1, _jpeg(f"e{ev}")))

    _run(scenario())
    # Победитель create потратил токен, проигравший вернул свой: у одного
    # из них на два новых инцидента остаётся один токен, у другого — два.
    assert outcomes.count(RelayMessageOutcome.DROPPED_RATE_LIMITED) == 1
    assert outcomes[:2] == [RelayMessageOutcome.PROCESSED] * 2
    assert frames[:2] in (["new_series", "new_frame"], ["new_frame", "new_series"])
    assert set(env.repo.rows[0].frame_sha) == {"1", "2"}
    assert len(env.intrusions) == 4
    assert env.security.enqueued == 4


@pytest.mark.parametrize("failing", ["create", "supersede"])
def test_failed_series_claim_refunds_rate_limit_token(failing, outcomes):
    env = _Env(burst=1.0)

    async def scenario():
        if failing == "supersede":
            env.repo.rows.append(_Row(id=1, locker_id="L1", relay_id=1001, event_id=42,
                                      series_uid=str(uuid.uuid4()), photos_cnt=3,
                                      first_seen=env.repo.now, frame_sha={"1": "x"}))
        env.repo.fail.add(failing)
        with pytest.raises(RuntimeError):
            await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a")))
        env.repo.fail.clear()
        await env.ingest.handle_message("photos", _frame(43, 1, _jpeg("b")))

    _run(scenario())
    assert outcomes == [RelayMessageOutcome.PROCESSED]
    assert env.repo.current_rows()[-1].event_id == 43


def test_concurrent_reset_yields_single_current_series():
    env = _Env()

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("old")))
        await asyncio.gather(
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("new"))),
            env.ingest.handle_message("photos", _frame(42, 1, _jpeg("new"))),
        )

    _run(scenario())
    assert len(env.repo.rows) == 2
    assert len(env.repo.current_rows()) == 1
    assert env.repo.calls.count("supersede") == 1
    assert len(env.intrusions) == 2


def test_stale_series_frame_starts_new_incident():
    env = _Env()

    async def scenario():
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a")))
        env.repo.now += 2 * _DAY
        await env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a")))

    _run(scenario())
    assert len(env.repo.rows) == 2
    assert len(env.intrusions) == 2


def test_maintenance_emits_pending_intrusion_and_purges():
    env = _Env()
    env.repo.fail.add("enqueue_intrusion")
    _run(env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))))
    assert env.repo.rows[0].intrusion_enqueued is None
    env.repo.fail.discard("enqueue_intrusion")
    env.repo.now += 120

    _run(env.ingest.run_photo_maintenance(retention_days=90))
    assert len(env.intrusions) == 1
    assert env.intrusions[0]["event_uid"] == \
        series_intrusion_uid(env.repo.rows[0].series_uid)
    assert env.repo.rows[0].intrusion_enqueued is not None
    assert env.repo.purged_with == [90]


def test_maintenance_steps_are_isolated():
    env = _Env()
    env.repo.fail.add("list_pending_intrusion")
    _run(env.ingest.run_photo_maintenance(retention_days=7))
    assert env.repo.purged_with == [7]

    env.repo.fail = {"purge_older_than"}
    _run(env.ingest.run_photo_maintenance(retention_days=7))
    assert env.repo.calls.count("list_pending_intrusion") == 2


def test_without_series_repo_frame_is_processed_but_not_published(outcomes):
    ingest = RelayIngest(repo=FakeLockers(), command_queue=None, command_results=None,
                         mqtt_client=None, media_producer=FakeMedia())
    _run(ingest.handle_message("photos", _frame(42, 1, _jpeg("a"))))
    assert outcomes == [RelayMessageOutcome.PROCESSED]


def test_unknown_relay_frame_dropped(outcomes):
    env = _Env()
    _run(env.ingest.handle_message("photos", _frame(42, 1, _jpeg("a"), device_id=7)))
    assert outcomes == [RelayMessageOutcome.DROPPED_UNKNOWN_RELAY]
    assert env.repo.calls == []


def test_series_uid_is_random_per_series():
    env = _Env()

    async def scenario():
        await env.ingest.handle_message("photos", _frame(1, 1, _jpeg("a")))
        await env.ingest.handle_message("photos", _frame(2, 1, _jpeg("a")))

    _run(scenario())
    uids = {r.series_uid for r in env.repo.rows}
    assert len(uids) == 2
    for u in uids:
        uuid.UUID(u)
