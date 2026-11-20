from datetime import UTC, datetime, timedelta

from app.observability import command_metrics
from shared import metrics
from shared.enums import CommandTransition


def _counter_value(command_type: str, transition: str) -> float:
    for m in metrics.command_lifecycle_total.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_lifecycle_total"
                and s.labels.get("command_type") == command_type
                and s.labels.get("transition") == transition
            ):
                return s.value
    return 0.0


def test_record_transition_increments() -> None:
    before = _counter_value("open", "queued")
    command_metrics.record_transition("open", CommandTransition.QUEUED)
    assert _counter_value("open", "queued") == before + 1


def test_record_queue_latency_skips_none_sent_at() -> None:
    # None sent_at не должен кидать — просто no-op
    command_metrics.record_queue_latency("open", datetime.now(UTC), None)


def test_record_ack_latency_observes_positive_delta() -> None:
    now = datetime.now(UTC)
    command_metrics.record_ack_latency("open", now - timedelta(seconds=2), now)
    total = 0.0
    for m in metrics.command_ack_latency_seconds.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_ack_latency_seconds_count"
                and s.labels.get("command_type") == "open"
            ):
                total = s.value
    assert total >= 1


def test_record_transition_never_raises_on_bad_input() -> None:
    # Плохой command_type не должен пробрасывать исключение наружу.
    command_metrics.record_transition("\x00bad", CommandTransition.QUEUED)


def test_record_queue_latency_never_raises_on_none_created() -> None:
    # None created_at (тип аннотирован как datetime, но защита best-effort)
    command_metrics.record_queue_latency(
        "open", None, datetime.now(UTC)  # type: ignore[arg-type]
    )


def test_record_ack_latency_skips_none_end_at() -> None:
    now = datetime.now(UTC)
    command_metrics.record_ack_latency("open", now, None)
