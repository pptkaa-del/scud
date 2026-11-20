"""Unit tests for the $SYS broker-metrics parser (never-raises) and the
scrape-time connected sampler. Lives in the locker_api suite because the
shared_tests image lacks prometheus_client (same rationale as
test_pool_metrics_endpoint)."""
from __future__ import annotations

from app.mqtt.sys_metrics import SysMetricsHandler, sample_mqtt_connected
from shared.metrics import (
    SERVICE_NAME,
    mqtt_broker_clients_connected,
    mqtt_broker_messages_dropped_total,
    mqtt_broker_messages_received_total,
    mqtt_broker_messages_sent_total,
    mqtt_connected,
)


def _gauge(metric: object) -> float:
    return metric.labels(service=SERVICE_NAME)._value.get()  # type: ignore[attr-defined]


def test_received_topic_sets_received_gauge() -> None:
    handler = SysMetricsHandler()
    handler.on_sys_message("$SYS/broker/messages/received", b"4321")
    assert _gauge(mqtt_broker_messages_received_total) == 4321.0


def test_sent_and_clients_connected_topics() -> None:
    handler = SysMetricsHandler()
    handler.on_sys_message("$SYS/broker/messages/sent", b"99")
    handler.on_sys_message("$SYS/broker/clients/connected", b"7")
    assert _gauge(mqtt_broker_messages_sent_total) == 99.0
    assert _gauge(mqtt_broker_clients_connected) == 7.0


def test_dropped_topic_sets_dropped_gauge() -> None:
    """Mosquitto 2.0 publishes the dropped-publish count under
    $SYS/broker/publish/messages/dropped (verified against the mosquitto(8)
    man page)."""
    handler = SysMetricsHandler()
    handler.on_sys_message("$SYS/broker/publish/messages/dropped", b"3")
    assert _gauge(mqtt_broker_messages_dropped_total) == 3.0


def test_parser_never_raises_on_garbage() -> None:
    """A malformed/forged $SYS frame must not raise — silent broker drop is
    security-relevant for an MQTT-primary СКУД, the ingest path must stay alive."""
    handler = SysMetricsHandler()
    handler.on_sys_message("$SYS/broker/messages/received", b"not-a-number")
    handler.on_sys_message("$SYS/broker/load/messages/received/1min", b"12.5")
    handler.on_sys_message("$SYS/broker/uptime", b"")
    handler.on_sys_message("$SYS/broker/version", b"mosquitto 2.0.18")
    handler.on_sys_message("$SYS/broker/clients/connected", b"\xff\xfe")
    # Contract is "does not raise"; no value assertions here.


def test_unmapped_topic_leaves_all_gauges_untouched() -> None:
    """A $SYS topic not in _SYS_MAP must call no setter."""
    before = (
        _gauge(mqtt_broker_messages_received_total),
        _gauge(mqtt_broker_messages_sent_total),
        _gauge(mqtt_broker_messages_dropped_total),
        _gauge(mqtt_broker_clients_connected),
    )
    SysMetricsHandler().on_sys_message("$SYS/broker/bytes/received", b"512")
    after = (
        _gauge(mqtt_broker_messages_received_total),
        _gauge(mqtt_broker_messages_sent_total),
        _gauge(mqtt_broker_messages_dropped_total),
        _gauge(mqtt_broker_clients_connected),
    )
    assert before == after


def test_sample_mqtt_connected_true_sets_one() -> None:
    class _Client:
        is_connected = True

    sample_mqtt_connected(_Client())
    assert _gauge(mqtt_connected) == 1.0


def test_sample_mqtt_connected_false_sets_zero() -> None:
    class _Client:
        is_connected = False

    sample_mqtt_connected(_Client())
    assert _gauge(mqtt_connected) == 0.0


def test_sample_mqtt_connected_none_client_sets_zero() -> None:
    sample_mqtt_connected(None)
    assert _gauge(mqtt_connected) == 0.0


def test_sample_mqtt_connected_swallows_raising_client() -> None:
    class _Boom:
        @property
        def is_connected(self) -> bool:
            raise RuntimeError("boom")

    sample_mqtt_connected(_Boom())  # must not raise
