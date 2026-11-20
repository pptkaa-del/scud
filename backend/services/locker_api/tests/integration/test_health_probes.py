"""Интеграционные тесты health-depth (/live, /ready) locker_api.

/ready перечисляет db (critical) + mqtt (soft, gmqtt-сессия к брокеру).
В compose-окружении брокер поднят (MQTT_HOST=mqtt) → mqtt=ok.
"""
import os

import requests

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8001")


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
    # mqtt-сессия установлена (брокер healthy в compose).
    assert body["mqtt"] == "ok"


def test_health_still_db_only() -> None:
    resp = requests.get(f"{BASE_URL}/health", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}


def test_ready_unavailable_when_db_down() -> None:
    """БД (critical) недоступна → status=unavailable, HTTP 503.

    Host-обёртка останавливает postgres_locker и выставляет SCUD_DB_DOWN=1.
    """
    import os as _os

    import pytest

    if _os.getenv("SCUD_DB_DOWN") != "1":
        pytest.skip("requires postgres_locker stopped (host wrapper sets SCUD_DB_DOWN=1)")

    resp = requests.get(f"{BASE_URL}/ready", timeout=10)
    assert resp.status_code == 503          # critical dep → kill-list status
    body = resp.json()
    assert body["status"] == "unavailable"
    assert body["db"] == "unavailable"


def test_ready_degraded_when_mqtt_down() -> None:
    """Брокер (soft) недоступен → status=degraded, HTTP 200 (НЕ 503).

    Host-обёртка останавливает mqtt и выставляет SCUD_MQTT_DOWN=1.
    """
    import os as _os

    import pytest

    if _os.getenv("SCUD_MQTT_DOWN") != "1":
        pytest.skip("requires mqtt stopped (host wrapper sets SCUD_MQTT_DOWN=1)")

    resp = requests.get(f"{BASE_URL}/ready", timeout=10)
    assert resp.status_code == 200          # soft dep → no kill
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["db"] == "ok"
    assert body["mqtt"] == "unavailable"
