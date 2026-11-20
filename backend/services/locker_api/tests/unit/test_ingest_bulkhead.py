import asyncio
from datetime import UTC, datetime

import pytest

from app.mqtt import dispatch as dispatch_mod
from app.mqtt.bulkhead import ByteBudgetBulkhead, IngestBulkhead
from app.mqtt.dispatch import make_photo_bulkhead, make_relay_dispatch
from app.mqtt.relay_codec import MAX_RELAY_PHOTO_BYTES
from shared.enums import RelayMessageOutcome


def test_bounds_concurrency():
    async def scenario():
        bh = IngestBulkhead(max_concurrency=2, max_pending=100)
        peak = 0
        current = 0

        async def handler():
            nonlocal peak, current
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1

        await asyncio.gather(*[bh.run(False, handler) for _ in range(10)])
        return peak

    assert asyncio.run(scenario()) == 2


def test_drops_when_pending_cap_exceeded():
    async def scenario():
        bh = IngestBulkhead(max_concurrency=1, max_pending=3)
        ran = 0

        async def slow():
            nonlocal ran
            ran += 1
            await asyncio.sleep(0.05)

        # Fire 6 non-exempt tasks; concurrency=1, pending cap=3 -> at most
        # ~3 admitted at once, the rest dropped (return False).
        results = await asyncio.gather(*[bh.run(False, slow) for _ in range(6)])
        dropped = results.count(False)
        return dropped, ran

    dropped, ran = asyncio.run(scenario())
    assert dropped >= 1          # overload actually shed load
    assert ran == 6 - dropped    # every admitted task ran exactly once


def test_exempt_never_dropped():
    async def scenario():
        bh = IngestBulkhead(max_concurrency=1, max_pending=1)
        ran = 0

        async def h():
            nonlocal ran
            ran += 1
            await asyncio.sleep(0.02)

        results = await asyncio.gather(*[bh.run(True, h) for _ in range(5)])
        return results, ran

    results, ran = asyncio.run(scenario())
    assert all(results)   # exempt (rep) always admitted
    assert ran == 5


def test_dispatch_shape_sheds_stats_but_not_rep():
    async def scenario():
        bh = IngestBulkhead(max_concurrency=1, max_pending=2)
        processed = []

        async def handler(tag):
            await asyncio.sleep(0.03)
            processed.append(tag)

        async def dispatch(is_rep, tag):
            return await bh.run(is_rep, lambda: handler(tag))

        return await asyncio.gather(
            dispatch(False, "s1"), dispatch(False, "s2"),
            dispatch(False, "s3"), dispatch(False, "s4"),
            dispatch(True, "rep1"),
        )

    results = asyncio.run(scenario())
    assert results[-1] is True          # rep always admitted
    assert results.count(False) >= 1    # some stats shed under overload


# ---------------------------------------------------------------------------
# Полоса фото: ByteBudgetBulkhead + маршрутизация в make_relay_dispatch
# ---------------------------------------------------------------------------


def test_byte_budget_rejects_over_budget_and_returns_bytes():
    async def scenario():
        bh = ByteBudgetBulkhead(max_concurrency=1, max_pending_bytes=100)
        release = asyncio.Event()

        async def blocked():
            await release.wait()

        first = asyncio.ensure_future(bh.run(60, blocked))
        await asyncio.sleep(0)
        assert bh.pending_bytes == 60
        assert await asyncio.wait_for(bh.run(50, blocked), timeout=1) is False
        release.set()
        assert await first is True
        assert bh.pending_bytes == 0
        assert await bh.run(100, lambda: asyncio.sleep(0)) is True

    asyncio.run(scenario())


def test_byte_budget_returns_bytes_when_handler_raises():
    async def scenario():
        bh = ByteBudgetBulkhead(max_concurrency=1, max_pending_bytes=10)

        async def boom():
            raise RuntimeError("x")

        with pytest.raises(RuntimeError):
            await bh.run(10, boom)
        assert bh.pending_bytes == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("kwargs", [
    {"max_concurrency": 0, "max_pending_bytes": 10},
    {"max_concurrency": 1, "max_pending_bytes": 0},
])
def test_byte_budget_rejects_non_positive(kwargs):
    with pytest.raises(ValueError):
        ByteBudgetBulkhead(**kwargs)


def test_photo_bulkhead_must_fit_largest_frame():
    with pytest.raises(ValueError):
        make_photo_bulkhead(2, MAX_RELAY_PHOTO_BYTES)
    assert make_photo_bulkhead(2, MAX_RELAY_PHOTO_BYTES + 20) is not None


class _BlockingIngest:
    def __init__(self):
        self.release = asyncio.Event()
        self.handled: list[str] = []
        self.received_at: dict[str, datetime | None] = {}

    async def handle_message(self, topic, payload, received_at=None):
        self.received_at[topic] = received_at
        if topic.startswith("photos"):
            await self.release.wait()
        self.handled.append(topic)


class _Sys:
    def __init__(self):
        self.seen: list[str] = []

    def on_sys_message(self, topic, payload):
        self.seen.append(topic)


def _outcomes(monkeypatch):
    seen: list[RelayMessageOutcome] = []
    monkeypatch.setattr(dispatch_mod, "record_relay_message", seen.append)
    return seen


def test_stat_and_rep_flow_while_photo_lane_is_saturated(monkeypatch):
    outcomes = _outcomes(monkeypatch)

    async def scenario():
        ingest = _BlockingIngest()
        dispatch = make_relay_dispatch(
            ingest, _Sys(), IngestBulkhead(max_concurrency=2, max_pending=10),
            ByteBudgetBulkhead(max_concurrency=2, max_pending_bytes=10_000),
        )
        photos = [asyncio.ensure_future(dispatch("photos/1001", b"x" * 100)) for _ in range(4)]
        await asyncio.sleep(0)
        await asyncio.wait_for(dispatch("stat/1001", b"s"), timeout=1)
        await asyncio.wait_for(dispatch("rep", b"r"), timeout=1)
        assert ingest.handled == ["stat/1001", "rep"]
        ingest.release.set()
        await asyncio.gather(*photos)
        assert ingest.handled.count("photos/1001") == 4

    asyncio.run(scenario())
    assert RelayMessageOutcome.DROPPED_OVERLOAD not in outcomes


def test_photo_flood_beyond_byte_budget_is_dropped(monkeypatch, caplog):
    outcomes = _outcomes(monkeypatch)

    async def scenario():
        ingest = _BlockingIngest()
        dispatch = make_relay_dispatch(
            ingest, _Sys(), IngestBulkhead(max_concurrency=2, max_pending=10),
            ByteBudgetBulkhead(max_concurrency=1, max_pending_bytes=250),
        )
        photos = [asyncio.ensure_future(dispatch("photos", b"x" * 100)) for _ in range(4)]
        await asyncio.sleep(0)
        ingest.release.set()
        await asyncio.gather(*photos)
        return ingest.handled

    handled = asyncio.run(scenario())
    assert len(handled) == 2
    assert outcomes.count(RelayMessageOutcome.DROPPED_OVERLOAD) == 2
    assert any("photo frame dropped" in r.getMessage() for r in caplog.records)


def test_sys_topics_bypass_ingest(monkeypatch):
    _outcomes(monkeypatch)

    async def scenario():
        ingest, sys_ = _BlockingIngest(), _Sys()
        dispatch = make_relay_dispatch(
            ingest, sys_, IngestBulkhead(1, 1), ByteBudgetBulkhead(1, 10),
        )
        await dispatch("$SYS/broker/clients/connected", b"3")
        return ingest.handled, sys_.seen

    handled, seen = asyncio.run(scenario())
    assert handled == []
    assert seen == ["$SYS/broker/clients/connected"]


def test_handler_error_recorded_and_raised(monkeypatch):
    outcomes = _outcomes(monkeypatch)

    class _Boom:
        async def handle_message(self, topic, payload, received_at=None):
            raise RuntimeError("db down")

    async def scenario():
        dispatch = make_relay_dispatch(
            _Boom(), _Sys(), IngestBulkhead(1, 1), ByteBudgetBulkhead(1, 10),
        )
        with pytest.raises(RuntimeError):
            await dispatch("photos", b"x")

    asyncio.run(scenario())
    assert outcomes == [RelayMessageOutcome.HANDLER_ERROR]


def test_received_at_is_taken_before_bulkhead_queue(monkeypatch):
    _outcomes(monkeypatch)

    async def scenario():
        ingest = _BlockingIngest()
        dispatch = make_relay_dispatch(
            ingest, _Sys(), IngestBulkhead(max_concurrency=1, max_pending=10),
            ByteBudgetBulkhead(max_concurrency=1, max_pending_bytes=10_000),
        )
        frames = [asyncio.ensure_future(dispatch(t, b"x")) for t in ("photos", "photos/1001")]
        await asyncio.sleep(0.05)
        released_at = datetime.now(UTC)
        ingest.release.set()
        await asyncio.gather(*frames)
        return released_at, ingest.received_at["photos/1001"]

    released_at, queued_received_at = asyncio.run(scenario())
    assert queued_received_at is not None
    assert queued_received_at < released_at
