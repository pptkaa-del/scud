"""Integration: the scud_mqtt_* gauges appear on locker_api /metrics.

Scrapes /metrics directly (the gateway does not proxy it) and asserts the four
broker series plus connected + relays_online are present with
service="locker_api". Values may be 0 (the broker may not have published a $SYS
frame yet, and the relays-online task may not have ticked) — presence, not a
specific value, is the contract here."""
from __future__ import annotations

import os
import re

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8001")
SERVICE = "locker_api"

_MQTT_SERIES = (
    "scud_mqtt_connected",
    "scud_mqtt_relays_online",
    "scud_mqtt_broker_messages_received_total",
    "scud_mqtt_broker_messages_sent_total",
    "scud_mqtt_broker_messages_dropped_total",
    "scud_mqtt_broker_clients_connected",
)


def _series_present(body: str, metric: str) -> bool:
    pattern = re.compile(
        rf'^{re.escape(metric)}\{{(?P<labels>[^}}]*)\}}\s+[0-9eE.+-]+$',
        re.MULTILINE,
    )
    return any(
        f'service="{SERVICE}"' in match.group("labels")
        for match in pattern.finditer(body)
    )


def test_metrics_endpoint_exposes_mqtt_gauges(api_client) -> None:
    resp = api_client.get("/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.text
    for metric in _MQTT_SERIES:
        assert _series_present(body, metric), (
            f"series {metric} with service={SERVICE} missing from /metrics"
        )
