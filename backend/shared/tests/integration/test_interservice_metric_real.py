"""Integration: реальный (не замоканный) межсервисный отказ двигает
scud_interservice_requests_total. Бьём GET по закрытому порту localhost —
ConnectionRefusedError ретраится (идемпотентный метод) и исчерпывается,
давая RETRY×(n-1) + EXHAUSTED×1. Живой event loop, без unittest.mock."""
from __future__ import annotations

import asyncio
import contextlib

from shared.enums import InterserviceOutcome
from shared.http import retry_client
from shared.http.retry_client import RetryHTTPClient
from shared.metrics import SERVICE_NAME as _METRICS_SERVICE_NAME, interservice_requests_total


def _exhausted_value(target: str) -> float:
    c = interservice_requests_total.labels(
        service=_METRICS_SERVICE_NAME,
        target=target, method="GET", outcome=InterserviceOutcome.EXHAUSTED.value,
    )
    return float(c._value.get())


def test_real_connection_failure_moves_exhausted_counter() -> None:
    assert retry_client._METRICS_AVAILABLE
    # 127.0.0.1:9 (discard) обычно закрыт → connection refused.
    url = "http://127.0.0.1:9/x"
    target = retry_client._extract_target(url)
    before = _exhausted_value(target)
    client = RetryHTTPClient(connect_timeout=0.5, request_timeout=0.5,
                             backoff_base=0.0, max_attempts=2)

    async def scenario() -> None:
        with contextlib.suppress(Exception):
            await client.fetch(url, method="GET")

    asyncio.run(scenario())
    assert _exhausted_value(target) == before + 1
