from prometheus_client import CollectorRegistry

from shared import metrics
from shared.enums import CommandTransition


def test_command_transition_enum_values() -> None:
    assert CommandTransition.QUEUED == "queued"
    assert CommandTransition.SENT == "sent"
    assert CommandTransition.SEND_FAILED == "send_failed"
    assert CommandTransition.EXECUTED == "executed"
    assert CommandTransition.FAILED == "failed"
    assert CommandTransition.ACK_TIMEOUT_RETRY == "ack_timeout_retry"
    assert CommandTransition.ACK_TIMEOUT_FAILED == "ack_timeout_failed"


def test_lifecycle_total_labels_register() -> None:
    metrics.command_lifecycle_total.labels(
        service="locker_api",
        command_type="open",
        transition=CommandTransition.QUEUED.value,
    ).inc()
    samples = {
        s.labels["transition"]
        for m in metrics.command_lifecycle_total.collect()
        for s in m.samples
        if s.name == "scud_command_lifecycle_total"
    }
    assert "queued" in samples


def test_latency_histograms_register() -> None:
    metrics.command_queue_latency_seconds.labels(
        service="locker_api", command_type="open"
    ).observe(0.5)
    metrics.command_ack_latency_seconds.labels(
        service="locker_api", command_type="open"
    ).observe(0.5)
    names = {m.name for m in metrics.command_queue_latency_seconds.collect()}
    assert "scud_command_queue_latency_seconds" in names
