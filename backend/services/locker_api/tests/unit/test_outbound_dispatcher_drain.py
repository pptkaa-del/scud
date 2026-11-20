import asyncio

import pytest

from app.services.outbound_dispatcher import OutboundDispatcher


class _FakeDB:
    pool_name = "test"


def _make(concurrency=4):
    d = OutboundDispatcher.__new__(OutboundDispatcher)
    d._db = _FakeDB()
    d._poll_interval = 0.05
    d._batch_size = 10
    d._concurrency = concurrency
    d._max_batch_bytes = None
    d._stop_event = asyncio.Event()
    d._history_api_url = "http://x"
    d._hmac_key = None
    d._last_retention_at = 0.0
    from datetime import timedelta
    d._retention = timedelta(days=7)
    d._retention_interval = 1e9  # never in-test
    return d


def test_delivers_batch_concurrently():
    async def scenario():
        d = _make(concurrency=4)
        peak = 0
        current = 0
        delivered = []

        async def fake_deliver(event):
            nonlocal peak, current
            current += 1
            peak = max(peak, current)
            await asyncio.sleep(0.02)
            current -= 1
            delivered.append(event)

        d._deliver = fake_deliver  # type: ignore[method-assign]
        batch = list(range(8))
        await d._deliver_batch(batch)
        return peak, sorted(delivered)

    peak, delivered = asyncio.run(scenario())
    assert peak == 4                       # bounded to concurrency
    assert delivered == list(range(8))     # all delivered exactly once


def test_no_sleep_between_nonempty_batches():
    async def scenario():
        d = _make()
        d._delivered_batches = 0
        # first poll returns a full-ish batch, second returns empty
        batches = [[1, 2, 3], []]

        async def fake_fetch(_n, **_kw):
            if not batches or batches[0] == []:
                d._stop_event.set()
                return []
            return batches.pop(0)

        async def fake_deliver_batch(events):
            d._delivered_batches += 1

        d._repo = type("R", (), {"fetch_pending": staticmethod(fake_fetch),
                                 "cleanup_delivered": staticmethod(
                                     lambda *_a, **_k: asyncio.sleep(0))})()
        d._deliver_batch = fake_deliver_batch  # type: ignore[method-assign]
        import time
        t0 = time.monotonic()
        await d.start()
        return time.monotonic() - t0

    elapsed = asyncio.run(scenario())
    # Non-empty batch must NOT trigger the poll_interval sleep (0.05s);
    # only the final empty batch may sleep once before stop is observed.
    assert elapsed < 0.2


def test_zero_concurrency_rejected():
    with pytest.raises(ValueError, match="concurrency must be > 0"):
        OutboundDispatcher(_FakeDB(), concurrency=-1)
