"""Drive a relay device through stat mutations that make the backend emit
device-asserted security events (power-loss, ERROR status, offline).

The relay-board protocol carries no intrusion/tamper/threshold sensors, so the
backend's *device-asserted* security events (locker_api ``ingest._handle_stat``)
are the only ones a relay double can raise:

  * ``power`` MAINS → BATTERY transition → "Питание переключено на резервную батарею (пропало основное питание)"
  * ``status`` == ERROR                   → "Устройство сообщило об ошибке (статус ERROR)"
  * ``status`` == OFFLINE                 → offline security event

Each is EDGE-TRIGGERED on the backend: it fires once on the transition and is
suppressed on repeats until the field returns to baseline. This scenario therefore
alternates every incident with a baseline-restoring step, so the edge re-arms and
events keep flowing on a fixed interval. The caller (transport) publishes a stat
after each :meth:`SecurityEventScenario.step`.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .device import RelayDevice

# Wire values (protocol §5). The emulator device stores these as raw ints; the
# backend maps them to DeviceStatus/PowerSource. Kept local so scenario.py does not
# depend on the backend package.
_STATUS_OFFLINE = 0
_STATUS_ONLINE = 1
_STATUS_ERROR = 2
_POWER_BATTERY = 0
_POWER_MAINS = 1


@dataclass(frozen=True, slots=True)
class Incident:
    """One step of the cycle: a label, whether the backend emits an event, and the
    device mutation to apply before the next stat is published."""

    label: str
    emits_event: bool
    mutate: Callable[[RelayDevice], None]


def _set_power(value: int) -> Callable[[RelayDevice], None]:
    def _apply(dev: RelayDevice) -> None:
        dev.power = value
    return _apply


def _set_status(value: int) -> Callable[[RelayDevice], None]:
    def _apply(dev: RelayDevice) -> None:
        dev.status = value
    return _apply


# Baseline (device defaults): power=MAINS, status=ONLINE. Every event step is
# followed by a restore step so the backend edge-trigger re-arms; one full cycle
# emits exactly three security events (power-loss, ERROR, offline).
_CYCLE: tuple[Incident, ...] = (
    Incident("power-loss (mains→battery)", True, _set_power(_POWER_BATTERY)),
    Incident("power-restore (battery→mains)", False, _set_power(_POWER_MAINS)),
    Incident("device ERROR status", True, _set_status(_STATUS_ERROR)),
    Incident("status-restore (online)", False, _set_status(_STATUS_ONLINE)),
    Incident("offline (status=0)", True, _set_status(_STATUS_OFFLINE)),
    Incident("online-restore (status=1)", False, _set_status(_STATUS_ONLINE)),
)


class SecurityEventScenario:
    """Cyclic stat-mutation driver over a single :class:`RelayDevice`.

    Call :meth:`step` on a fixed interval; each call mutates the device and returns
    the incident just applied. The device is only mutated — publishing the resulting
    stat is the caller's responsibility.
    """

    def __init__(self, device: RelayDevice) -> None:
        self._dev = device
        self._i = 0

    def step(self) -> Incident:
        incident = _CYCLE[self._i % len(_CYCLE)]
        incident.mutate(self._dev)
        self._i += 1
        return incident

    @property
    def cycle_length(self) -> int:
        return len(_CYCLE)

    @property
    def events_per_cycle(self) -> int:
        return sum(1 for inc in _CYCLE if inc.emits_event)
