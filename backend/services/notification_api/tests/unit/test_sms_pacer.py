import asyncio
import heapq
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.sms_gateway_client import SmsGatewayClient
from shared.http import UpstreamUnavailableError

OK = b"Commit successfully"


class VirtualTime:
    """Виртуальные часы: sleep не спит, время прыгает к ближайшему будильнику."""

    def __init__(self) -> None:
        self.now = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = 0
        self.sleep_calls: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleep_calls.append(delay)
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._seq += 1
        heapq.heappush(self._sleepers, (self.now + delay, self._seq, fut))
        await fut

    async def run(self, *coros):
        tasks = [asyncio.ensure_future(c) for c in coros]
        while not all(t.done() for t in tasks):
            for _ in range(10):
                await asyncio.sleep(0)
            if self._sleepers and not all(t.done() for t in tasks):
                wake, _, fut = heapq.heappop(self._sleepers)
                self.now = max(self.now, wake)
                fut.set_result(None)
        return await asyncio.gather(*tasks, return_exceptions=True)


def _setup(vt: VirtualTime, *, interval=10.0, max_wait=10.0):
    sent: list[tuple[float, str]] = []

    async def fetch(url, method="GET"):
        resp = MagicMock()
        resp.code = 200
        resp.body = OK
        dest = url.split("destination=")[1].split("&")[0]
        sent.append((vt.now, dest))
        return resp

    http = MagicMock()
    http.fetch = AsyncMock(side_effect=fetch)
    client = SmsGatewayClient(
        http=http, base_url="http://gw", account="u", password="p",
        min_interval_s=interval, max_queue_wait_s=max_wait,
        clock=vt.monotonic, sleep=vt.sleep,
    )
    return client, http, sent


def _run(vt, *coros):
    return asyncio.run(vt.run(*coros))


def test_concurrent_senders_are_served_fifo_each_waits_only_its_slot():
    vt = VirtualTime()
    client, _, sent = _setup(vt, max_wait=100.0)
    results = _run(
        vt,
        client.send_text("+79000000001", "a"),
        client.send_text("+79000000002", "b"),
        client.send_text("+79000000003", "c"),
    )
    assert results == [None, None, None]
    assert sent == [
        (0.0, "%2B79000000001"), (10.0, "%2B79000000002"), (20.0, "%2B79000000003"),
    ]
    assert sorted(vt.sleep_calls) == [10.0, 20.0]


def test_idle_sender_does_not_wait():
    vt = VirtualTime()
    client, _, sent = _setup(vt)
    _run(vt, client.send_text("+79000000001", "a"))
    vt.now = 500.0
    _run(vt, client.send_text("+79000000002", "b"))
    assert [t for t, _ in sent] == [0.0, 500.0]
    assert vt.sleep_calls == []


def test_sender_over_queue_bound_is_rejected_without_gateway_call():
    vt = VirtualTime()
    client, http, sent = _setup(vt, max_wait=10.0)
    results = _run(
        vt,
        client.send_text("+79000000001", "a"),
        client.send_text("+79000000002", "b"),
        client.send_text("+79000000003", "c"),
    )
    assert results[0] is None
    assert results[1] is None
    assert isinstance(results[2], UpstreamUnavailableError)
    assert results[2].upstream == "sms_gateway"
    assert http.fetch.call_count == 2
    assert [d for _, d in sent] == ["%2B79000000001", "%2B79000000002"]


def test_rejected_sender_does_not_reserve_slot():
    vt = VirtualTime()
    client, _, sent = _setup(vt, max_wait=10.0)
    _run(
        vt,
        client.send_text("+79000000001", "a"),
        client.send_text("+79000000002", "b"),
        client.send_text("+79000000003", "c"),
    )
    assert vt.now == 10.0
    _run(vt, client.send_text("+79000000004", "d"))
    assert sent[-1] == (20.0, "%2B79000000004")


def test_multipart_over_bound_is_all_or_nothing():
    vt = VirtualTime()
    client, http, _ = _setup(vt, max_wait=10.0)
    results = _run(
        vt,
        client.send_text("+79000000001", "a"),
        client.send_text("+79000000002", "b"),
        client.send_text("+79000000003", "я" * 141),
    )
    assert isinstance(results[2], UpstreamUnavailableError)
    assert http.fetch.call_count == 2


def test_multipart_reserves_all_slots_atomically():
    vt = VirtualTime()
    client, _, sent = _setup(vt, max_wait=100.0)
    _run(
        vt,
        client.send_text("+79000000001", "я" * 141),
        client.send_text("+79000000002", "z"),
    )
    assert sent == [
        (0.0, "%2B79000000001"), (10.0, "%2B79000000001"),
        (20.0, "%2B79000000001"), (30.0, "%2B79000000002"),
    ]


def test_multipart_on_empty_queue_is_not_rejected_by_part_count():
    vt = VirtualTime()
    client, http, _ = _setup(vt, max_wait=10.0)
    results = _run(vt, client.send_text("+79000000001", "я" * 141))
    assert results == [None]
    assert http.fetch.call_count == 3


def test_zero_interval_disables_pacing():
    vt = VirtualTime()
    client, http, _ = _setup(vt, interval=0.0, max_wait=0.0)
    results = _run(
        vt,
        client.send_text("+79000000001", "a"),
        client.send_text("+79000000002", "b"),
        client.send_text("+79000000003", "c"),
    )
    assert results == [None, None, None]
    assert vt.sleep_calls == []
    assert http.fetch.call_count == 3


def test_negative_queue_bound_rejected_at_construction():
    with pytest.raises(ValueError):
        SmsGatewayClient(
            http=MagicMock(), base_url="http://gw", account="u", password="p",
            max_queue_wait_s=-1.0,
        )
