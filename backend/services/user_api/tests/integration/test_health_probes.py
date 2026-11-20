"""Интеграционные тесты health-depth (/live, /ready) user_api.

/ready перечисляет db (critical) + redis (soft). Redis-сбой → degraded+200,
а не 503 (graceful-деградация инвалидации кэша, не теряет трафик).
"""
import os

import requests

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8000")


def test_live_returns_alive() -> None:
    resp = requests.get(f"{BASE_URL}/live", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"status": "alive"}


def test_ready_healthy_ok() -> None:
    resp = requests.get(f"{BASE_URL}/ready", timeout=5)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["db"] == "ok"
    assert body["redis"] == "ok"


def test_health_still_db_only() -> None:
    resp = requests.get(f"{BASE_URL}/health", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


def test_ready_degraded_when_redis_down() -> None:
    """Redis-сбой → soft-провал: degraded+200, db остаётся ok, redis=unavailable.

    Управляется снаружи: SCUD_REDIS_DOWN=1 выставляется host-обёрткой, которая
    останавливает контейнер redis перед прогоном и поднимает после. Без флага
    тест скипается (обычный CI-прогон со здоровым redis).
    """
    import os as _os

    import pytest

    if _os.getenv("SCUD_REDIS_DOWN") != "1":
        pytest.skip("requires redis stopped (host wrapper sets SCUD_REDIS_DOWN=1)")

    resp = requests.get(f"{BASE_URL}/ready", timeout=5)
    assert resp.status_code == 200          # НЕ 503 — soft dep
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["redis"] == "unavailable"
