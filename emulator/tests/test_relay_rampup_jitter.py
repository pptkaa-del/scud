"""#25 — fleet ramp-up + heartbeat jitter.

A fleet of N relays that connect simultaneously and heartbeat on a fixed interval
sends ~N stats at once every interval (a thundering herd that wedges backend ingest).
Ramp-up staggers connections; jitter desyncs the recurring heartbeat bursts.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from scud_relay.device import RelayDevice
from scud_relay.mqtt_transport import RelayMqttTransport


def _make_transport(device_id: int = 1, interval: float = 300.0) -> RelayMqttTransport:
    transport = RelayMqttTransport.__new__(RelayMqttTransport)
    transport._dev = RelayDevice(device_id=device_id, version=2)
    transport._host = "localhost"
    transport._port = 1883
    transport._keepalive = 95
    transport._status_interval_s = interval
    transport._client = MagicMock()
    return transport


class TestHeartbeatJitter:
    def test_delay_within_15_percent_band(self):
        t = _make_transport(interval=300.0)
        for _ in range(500):
            d = t._next_heartbeat_delay()
            assert 0.85 * 300.0 <= d <= 1.15 * 300.0

    def test_jitter_produces_distinct_delays(self):
        """Distinct delays across draws → heartbeats desync (not a fixed interval)."""
        t = _make_transport(interval=300.0)
        delays = {round(t._next_heartbeat_delay(), 9) for _ in range(64)}
        assert len(delays) > 1


class TestRampUp:
    async def test_ramp_up_starts_all_devices(self, monkeypatch):
        """Staggered start must still bring up every device (no drops)."""
        import scud_relay.fleet as fleet_mod

        started: list[int] = []

        class _StubTransport:
            def __init__(self, dev: RelayDevice, host: str, port: int,
                         **_kwargs) -> None:
                self._id = dev.device_id

            async def run(self) -> None:
                started.append(self._id)

        monkeypatch.setattr(fleet_mod, "RelayMqttTransport", _StubTransport)
        # step = 0.05 / 5 = 0.01s — tiny real stagger, all done within ~0.04s
        await fleet_mod.run_fleet(count=5, host="h", port=1883, base_id=1,
                                  ramp_up_seconds=0.05)
        assert sorted(started) == [1, 2, 3, 4, 5]

    async def test_no_ramp_up_is_immediate(self, monkeypatch):
        """ramp_up_seconds=0 keeps the prior all-at-once behavior (step=0)."""
        import scud_relay.fleet as fleet_mod

        started: list[int] = []

        class _StubTransport:
            def __init__(self, dev: RelayDevice, host: str, port: int,
                         **_kwargs) -> None:
                self._id = dev.device_id

            async def run(self) -> None:
                started.append(self._id)

        monkeypatch.setattr(fleet_mod, "RelayMqttTransport", _StubTransport)
        await fleet_mod.run_fleet(count=3, host="h", port=1883, base_id=1)
        assert sorted(started) == [1, 2, 3]
