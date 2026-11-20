"""Unit: scud_mqtt_* gauges и never-raises сеттеры в shared.metrics."""
from __future__ import annotations

import unittest

from prometheus_client import generate_latest

from shared.metrics import (
    SERVICE_NAME,
    MqttBrokerStat,
    mqtt_broker_clients_connected,
    mqtt_broker_messages_dropped_total,
    mqtt_broker_messages_received_total,
    mqtt_broker_messages_sent_total,
    mqtt_connected,
    mqtt_relays_online,
    set_mqtt_broker_stat,
    set_mqtt_connected,
    set_mqtt_relays_online,
)

_EXPECTED_SERIES = (
    "scud_mqtt_connected",
    "scud_mqtt_relays_online",
    "scud_mqtt_broker_messages_received_total",
    "scud_mqtt_broker_messages_sent_total",
    "scud_mqtt_broker_messages_dropped_total",
    "scud_mqtt_broker_clients_connected",
)


class TestMqttMetrics(unittest.TestCase):
    def test_all_series_exported(self) -> None:
        body = generate_latest().decode()
        for name in _EXPECTED_SERIES:
            self.assertIn(name, body)

    def test_all_labelled_service_only(self) -> None:
        for g in (
            mqtt_connected, mqtt_relays_online,
            mqtt_broker_messages_received_total, mqtt_broker_messages_sent_total,
            mqtt_broker_messages_dropped_total, mqtt_broker_clients_connected,
        ):
            self.assertEqual(g._labelnames, ("service",))

    def test_set_mqtt_connected_maps_bool_to_one_zero(self) -> None:
        set_mqtt_connected(True)
        self.assertEqual(
            mqtt_connected.labels(service=SERVICE_NAME)._value.get(), 1.0,
        )
        set_mqtt_connected(False)
        self.assertEqual(
            mqtt_connected.labels(service=SERVICE_NAME)._value.get(), 0.0,
        )

    def test_set_mqtt_relays_online_sets_value(self) -> None:
        set_mqtt_relays_online(42)
        self.assertEqual(
            mqtt_relays_online.labels(service=SERVICE_NAME)._value.get(), 42.0,
        )

    def test_set_mqtt_broker_stat_mirrors_value(self) -> None:
        set_mqtt_broker_stat(MqttBrokerStat.MESSAGES_RECEIVED, 1000)
        self.assertEqual(
            mqtt_broker_messages_received_total.labels(
                service=SERVICE_NAME,
            )._value.get(),
            1000.0,
        )
        set_mqtt_broker_stat(MqttBrokerStat.CLIENTS_CONNECTED, 7)
        self.assertEqual(
            mqtt_broker_clients_connected.labels(
                service=SERVICE_NAME,
            )._value.get(),
            7.0,
        )

    def test_setters_never_raise_on_bad_input(self) -> None:
        # Production-flow не должен падать из-за метрик: нечисловой/None вход
        # глотается (зеркалит update_pool_gauges).
        set_mqtt_relays_online(None)        # type: ignore[arg-type]
        set_mqtt_broker_stat(MqttBrokerStat.MESSAGES_SENT, "oops")  # type: ignore[arg-type]
        set_mqtt_connected(object())        # type: ignore[arg-type]
        # No exception → pass.


if __name__ == "__main__":
    unittest.main()
