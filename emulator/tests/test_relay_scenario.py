"""Security-event scenario: cyclic stat mutations that drive backend security events.

Verifies the deterministic mutation sequence (power-loss → ERROR → offline, each
followed by a baseline restore) so the backend edge-triggers re-arm and events keep
flowing. Pure logic — no broker, no network.
"""
from __future__ import annotations

from scud_relay.device import RelayDevice
from scud_relay.scenario import SecurityEventScenario

# Wire baselines (protocol §5): device defaults are MAINS power / ONLINE status.
_POWER_BATTERY, _POWER_MAINS = 0, 1
_STATUS_OFFLINE, _STATUS_ONLINE, _STATUS_ERROR = 0, 1, 2


def _device() -> RelayDevice:
    return RelayDevice(device_id=1, version=2)


class TestSecurityEventScenario:
    def test_cycle_returns_to_baseline(self):
        dev = _device()
        base_power, base_status = dev.power, dev.status
        scenario = SecurityEventScenario(dev)
        for _ in range(scenario.cycle_length):
            scenario.step()
        assert dev.power == base_power == _POWER_MAINS
        assert dev.status == base_status == _STATUS_ONLINE

    def test_field_and_event_sequence(self):
        dev = _device()
        scenario = SecurityEventScenario(dev)
        seen = []
        for _ in range(scenario.cycle_length):
            inc = scenario.step()
            seen.append((inc.emits_event, dev.power, dev.status))
        # power-loss, restore, ERROR, restore, offline, restore.
        assert [s[0] for s in seen] == [True, False, True, False, True, False]
        assert seen[0][1] == _POWER_BATTERY   # power → battery (event)
        assert seen[1][1] == _POWER_MAINS     # power restored
        assert seen[2][2] == _STATUS_ERROR    # status → ERROR (event)
        assert seen[3][2] == _STATUS_ONLINE   # status restored
        assert seen[4][2] == _STATUS_OFFLINE  # status → offline (event)
        assert seen[5][2] == _STATUS_ONLINE   # status restored

    def test_three_events_per_cycle(self):
        scenario = SecurityEventScenario(_device())
        assert scenario.events_per_cycle == 3

    def test_loop_is_deterministic_across_cycles(self):
        scenario = SecurityEventScenario(_device())
        first = [scenario.step().label for _ in range(scenario.cycle_length)]
        second = [scenario.step().label for _ in range(scenario.cycle_length)]
        assert first == second

    def test_build_stat_reflects_mutation(self):
        """The mutated field actually changes the encoded stat bytes."""
        dev = _device()
        before = dev.build_stat()
        SecurityEventScenario(dev).step()  # power → battery
        assert dev.build_stat() != before
