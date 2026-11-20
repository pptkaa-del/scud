"""Общие хелперы для сценариев релейного MQTT-транспорта: создание mqtt-шкафа
через gateway API и генерация relay_id вне UNIQUE-диапазонов, занятых
архивированными шкафами.
"""

from __future__ import annotations

import random

from conftest import GATEWAY_URL
from utils.api_client import GatewayClient


def _random_relay_id() -> int:
    """Случайный relay_id из широкого диапазона (partial unique index по relay_id).

    Диапазон большой, чтобы избежать коллизий между прогонами на сохранённых
    volume-ах (шкафы soft-deleted, но relay_id остаётся занятым до hard-delete).
    """
    return random.randint(1_000_000, 9_999_999)


def _create_mqtt_locker(gateway: GatewayClient, relay_id: int) -> dict:
    """Создать + провизионировать mqtt-шкаф через gateway (роль administrator).

    api_client.create_locker не умеет transport/relay_id, поэтому шлём raw POST.
    """
    resp = gateway._session.post(  # noqa: SLF001 - reuse authed session
        f"{GATEWAY_URL}/api/v1/lockers",
        json={
            "inventory_number": f"RELAY-E2E-{relay_id}",
            "transport": "mqtt",
            "relay_id": relay_id,
        },
        timeout=30,
    )
    assert resp.status_code == 201, (
        f"create mqtt locker failed: {resp.status_code} {resp.text}"
    )
    return resp.json()
