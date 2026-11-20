"""Интеграционный тест: scud_redis_up появляется на /metrics ACM.

Требует запущенного ACM с доступным Redis.
"""
from __future__ import annotations

import os
import time

import requests

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8004")

_POLL_TIMEOUT = 25
_POLL_INTERVAL = 1


def test_redis_up_metric_present_and_one() -> None:
    """scud_redis_up{service="acm"} должен появиться на /metrics со значением 1."""
    deadline = time.monotonic() + _POLL_TIMEOUT
    last_body = ""
    while time.monotonic() < deadline:
        try:
            resp = requests.get(f"{BASE_URL}/metrics", timeout=5)
            if resp.status_code == 200:
                last_body = resp.text
                if "scud_redis_up" in last_body:
                    break
        except requests.RequestException:
            pass
        time.sleep(_POLL_INTERVAL)
    else:
        raise AssertionError(
            f"scud_redis_up not found in /metrics after {_POLL_TIMEOUT}s.\n"
            f"Last body snippet: {last_body[:500]!r}"
        )

    # Метрика должна содержать service="acm" с значением 1
    found_one = False
    for line in last_body.splitlines():
        if line.startswith("#"):
            continue
        if "scud_redis_up" in line and 'service="acm"' in line:
            value_str = line.rsplit(" ", 1)[-1].strip()
            assert value_str == "1.0", (
                f"Expected scud_redis_up{{service=\"acm\"}} = 1.0, got {value_str!r}.\n"
                f"Line: {line!r}"
            )
            found_one = True
            break

    assert found_one, (
        f"No scud_redis_up line with service=\"acm\" found.\n"
        f"Body snippet: {last_body[:500]!r}"
    )
