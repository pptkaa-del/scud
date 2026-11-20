"""Adaptive timing helpers for E2E scenarios.

Reads STATE_MONITOR_CHECK_INTERVAL_S and DEFAULT_TELEMETRY_INTERVAL_S from
environment (set via tests/system/.env.fast for fast E2E mode). Production
defaults (60s/30s) are preserved.

Usage:
    from utils.timing import state_monitor_cycles, telemetry_cycles
    time.sleep(state_monitor_cycles(2, buffer_s=2))  # wait 2 cycles + 2s buffer
"""
import os

# Match defaults to production (matches main docker-compose.yaml)
STATE_MONITOR_INTERVAL_S = int(os.environ.get("STATE_MONITOR_CHECK_INTERVAL_S", "60"))
TELEMETRY_INTERVAL_S = int(os.environ.get("DEFAULT_TELEMETRY_INTERVAL_S", "30"))


def state_monitor_cycles(n: int = 1, buffer_s: float = 2.0) -> float:
    """Wait time для n циклов state_monitor + запас."""
    return STATE_MONITOR_INTERVAL_S * n + buffer_s


def telemetry_cycles(n: int = 1, buffer_s: float = 1.0) -> float:
    """Wait time для n циклов телеметрии + запас."""
    return TELEMETRY_INTERVAL_S * n + buffer_s
