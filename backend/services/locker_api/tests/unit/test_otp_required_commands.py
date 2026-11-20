"""OTP_REQUIRED_COMMANDS — privileged-command set guarded by step-up 2FA."""
from __future__ import annotations

from shared.access_control.commands import OTP_REQUIRED_COMMANDS


def test_open_requires_otp() -> None:
    assert "open" in OTP_REQUIRED_COMMANDS


def test_reboot_requires_otp() -> None:
    assert "reboot" in OTP_REQUIRED_COMMANDS


def test_config_not_in_otp_set() -> None:
    """`config` выведен из эксплуатации — не требует OTP (и вообще недоступен)."""
    assert "config" not in OTP_REQUIRED_COMMANDS


def test_close_does_not_require_otp() -> None:
    """`close` — explicit opt-out per design (forced lock should remain fast)."""
    assert "close" not in OTP_REQUIRED_COMMANDS


def test_relay_commands_require_otp() -> None:
    assert {"relay_on", "relay_off", "relay_restart"} <= OTP_REQUIRED_COMMANDS


def test_frozen_set_type() -> None:
    """Immutable to prevent accidental runtime mutation."""
    assert isinstance(OTP_REQUIRED_COMMANDS, frozenset)
    assert all(isinstance(x, str) for x in OTP_REQUIRED_COMMANDS)
