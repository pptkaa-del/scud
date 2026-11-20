"""Mirror Mosquitto $SYS/broker/# counters onto the scud_mqtt_broker_* gauges
and sample scud_mqtt_connected from the live gmqtt session at scrape time.

locker_api owns the single gmqtt session (shared.mqtt_client.MqttClient), which
exposes exactly one message handler. Rather than stand up a second broker user
just for monitoring, the existing handler branches: $SYS frames come here, all
other topics stay on the relay-ingest path. The parser NEVER raises — a forged
or malformed $SYS frame must not break telemetry ingest on an MQTT-primary
СКУД, where a silent broker drop is itself a security signal.
"""
from __future__ import annotations

import logging
from typing import Any

from shared.metrics import MqttBrokerStat, set_mqtt_broker_stat, set_mqtt_connected

logger = logging.getLogger(__name__)

# Exact Mosquitto 2.0 $SYS topic -> MqttBrokerStat member. Only the four
# cumulative counters we re-expose are mapped; every other $SYS topic
# (load/* rate averages, bytes/*, text topics like version/uptime) is ignored.
# The dropped-publish counter lives under publish/messages/dropped (verified
# against the mosquitto(8) man page), NOT messages/publish/dropped.
_SYS_MAP: dict[str, MqttBrokerStat] = {
    "$SYS/broker/messages/received": MqttBrokerStat.MESSAGES_RECEIVED,
    "$SYS/broker/messages/sent": MqttBrokerStat.MESSAGES_SENT,
    "$SYS/broker/publish/messages/dropped": MqttBrokerStat.MESSAGES_DROPPED,
    "$SYS/broker/clients/connected": MqttBrokerStat.CLIENTS_CONNECTED,
}


class SysMetricsHandler:
    """Routes one $SYS/broker/# frame onto its MqttBrokerStat gauge."""

    def on_sys_message(self, topic: str, payload: bytes) -> None:
        stat = _SYS_MAP.get(topic)
        if stat is None:
            return
        try:
            value = float(payload.decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            # Forged/garbage payload — ignore, never break ingest.
            logger.debug("ignoring non-numeric $SYS frame on %s", topic)
            return
        set_mqtt_broker_stat(stat, value)


def sample_mqtt_connected(mqtt_client: Any) -> None:
    """Scrape-time sampler for scud_mqtt_connected.

    Registered via shared.metrics.register_metric_sampler; called from
    MetricsHandler.get() before generate_latest(). Reads MqttClient.is_connected
    (Task A2) — a pure in-memory read, no I/O. Never raises: a None client or a
    raising property yields connected=0 (mirrors update_pool_gauges' robustness).
    """
    try:
        connected = bool(getattr(mqtt_client, "is_connected", False))
    except Exception:
        logger.debug("failed to read mqtt_client.is_connected", exc_info=True)
        connected = False
    set_mqtt_connected(connected)
