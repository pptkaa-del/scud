"""Boot-хуки команд: TTL-drain до reset_stale_sent_commands, fail-closed COMMAND_TTL_S."""
import asyncio
import logging
import os
from unittest import mock

import pytest

from app import main as main_module
from app.main import build_command_expiry, run_command_boot_hooks


def _run_hooks(drain_side_effect=None):
    order: list[str] = []
    expiry = mock.MagicMock()

    async def drain(_repo):
        order.append("drain")
        if drain_side_effect is not None:
            raise drain_side_effect
        return 3

    async def reset(_db):
        order.append("reset")
        return 0

    expiry.drain = drain
    with mock.patch.object(main_module, "reset_stale_sent_commands", side_effect=reset):
        asyncio.run(run_command_boot_hooks(mock.MagicMock(), expiry))
    return order


def test_boot_drains_before_reset_stale_sent():
    assert _run_hooks() == ["drain", "reset"]


def test_boot_drain_failure_is_logged_and_boot_continues(caplog):
    with caplog.at_level(logging.ERROR, logger="app.main"):
        order = _run_hooks(drain_side_effect=RuntimeError("db down"))
    assert order == ["drain", "reset"]
    assert any("Command TTL sweep on startup failed" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_ttl_fails_startup(value):
    with mock.patch.dict(os.environ, {"COMMAND_TTL_S": value}), pytest.raises(ValueError):
        build_command_expiry(5)


def test_default_ttl_is_60():
    env = {k: v for k, v in os.environ.items() if k != "COMMAND_TTL_S"}
    with mock.patch.dict(os.environ, env, clear=True):
        assert build_command_expiry(5).ttl_s == 60


@pytest.mark.parametrize(("ttl", "ack_min", "warns"), [
    ("60", 1, False),
    ("61", 1, True),
    ("60", 5, False),
    ("600", 5, True),
])
def test_overlap_warning_only_when_ttl_exceeds_ack_timeout(caplog, ttl, ack_min, warns):
    with mock.patch.dict(os.environ, {"COMMAND_TTL_S": ttl}), \
            caplog.at_level(logging.WARNING, logger="app.main"):
        build_command_expiry(ack_min)
    warned = any("exceeds ack timeout" in r.getMessage() for r in caplog.records)
    assert warned is warns
