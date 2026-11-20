"""CommandExpiry: валидация TTL, аргументы sweep, метрика ttl_expired, drain батчами."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.command_expiry import (
    _SWEEP_BATCH,
    COMMAND_TTL_FAILURE_REASON,
    CommandExpiry,
)
from app.services.history_producers.command_history_producer import CommandHistoryProducer
from shared import metrics


def _ttl_expired_count(command_type: str) -> float:
    for m in metrics.command_lifecycle_total.collect():
        for s in m.samples:
            if (
                s.name == "scud_command_lifecycle_total"
                and s.labels.get("command_type") == command_type
                and s.labels.get("transition") == "ttl_expired"
            ):
                return s.value
    return 0.0


def _cmd(i: int, command_type: str = "reboot") -> SimpleNamespace:
    return SimpleNamespace(id=f"c{i}", type=command_type, locker_id="L1")


@pytest.mark.parametrize("ttl", [0, -5])
def test_non_positive_ttl_rejected(ttl):
    with pytest.raises(ValueError):
        CommandExpiry(ttl)


def test_sweep_passes_exact_arguments_and_counts_metric():
    repo = MagicMock()
    repo.expire_stale_commands = AsyncMock(return_value=[_cmd(1, "config"), _cmd(2, "config")])
    before = _ttl_expired_count("config")

    rows = asyncio.run(CommandExpiry(42).sweep(repo))

    assert len(rows) == 2
    repo.expire_stale_commands.assert_awaited_once_with(
        ttl_s=42,
        error_message=COMMAND_TTL_FAILURE_REASON,
        outbox_channel=CommandHistoryProducer.CHANNEL,
        outbox_endpoint=CommandHistoryProducer.ENDPOINT,
        limit=_SWEEP_BATCH,
    )
    assert _ttl_expired_count("config") - before == 2


def test_drain_loops_while_batch_full():
    repo = MagicMock()
    full = [_cmd(i) for i in range(_SWEEP_BATCH)]
    repo.expire_stale_commands = AsyncMock(side_effect=[full, full, [_cmd(0)]])

    total = asyncio.run(CommandExpiry(60).drain(repo))

    assert total == 2 * _SWEEP_BATCH + 1
    assert repo.expire_stale_commands.await_count == 3


def test_drain_stops_on_empty_batch():
    repo = MagicMock()
    repo.expire_stale_commands = AsyncMock(return_value=[])
    assert asyncio.run(CommandExpiry(60).drain(repo)) == 0
    assert repo.expire_stale_commands.await_count == 1


def test_ttl_property():
    assert CommandExpiry(60).ttl_s == 60
