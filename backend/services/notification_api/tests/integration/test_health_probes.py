"""Интеграционные тесты health-depth (/live, /ready) notification_api. db-only.

Max-бот в /ready НЕ пробивается (он — гейдж scud_max_bot_reachable).
"""
import os

import requests

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8005")


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
    assert "mqtt" not in body
    assert "max" not in body


def test_health_still_db_only() -> None:
    resp = requests.get(f"{BASE_URL}/health", timeout=5)
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "db": "ok"}
