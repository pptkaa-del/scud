"""Headless relay-board model mirroring the original RelayEmulator behavior."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

from .protocol import Com


class Cmd(IntEnum):
    NONE = 0
    RELAY_ON = 1
    RELAY_OFF = 2
    RELAY_REBOOT = 3
    SEND_STATUS = 4
    LOCK_OPEN = 5
    LOCK_CLOSE = 6


class Rep(IntEnum):
    SUCCESS = 1
    FAILURE = 2
    NOT_SUPPORTED = 3


class Door(IntEnum):
    UNDEFINED = 0
    OPEN = 1
    CLOSED = 2


@dataclass(slots=True)
class CommandOutcome:
    result: int           # REP_* (meaningful only if rep=True)
    rep: bool             # whether to publish a rep
    publish_stat: bool    # whether to publish an updated stat


@dataclass
class RelayDevice:
    device_id: int
    version: int = 2
    relay_en: int = 1
    door: int = Door.CLOSED
    power: int = 1
    battery: int = 100
    net_type: int = 1
    status: int = 1
    signal: int = 25
    alarm: int = 0
    relays: list[int] = field(default_factory=lambda: [0, 0, 0, 0])
    # GUI-only "Lock" checkboxes in the C++ reference. A locked relay/door rejects
    # control commands with REP_FAILURE and does not change state. Headless runs
    # leave these empty/False (nothing locked -> normal SUCCESS behavior); tests
    # can populate them to exercise the FAILURE path.
    locked_relays: set[int] = field(default_factory=set)  # 1..4
    door_locked: bool = False

    def apply_command(self, com: Com) -> CommandOutcome:
        cmd = com.command
        # CMD_NONE: C++ processCommand returns immediately, publishing nothing.
        if cmd == Cmd.NONE:
            return CommandOutcome(result=Rep.SUCCESS, rep=False, publish_stat=False)
        if cmd == Cmd.SEND_STATUS:
            return CommandOutcome(result=Rep.SUCCESS, rep=False, publish_stat=True)
        if cmd in (Cmd.LOCK_OPEN, Cmd.LOCK_CLOSE):
            if self.version != 2:
                return CommandOutcome(Rep.NOT_SUPPORTED, rep=True, publish_stat=False)
            # C++ changingStatDoor: if the door's GUI "Lock" is set, the control
            # checkbox is left untouched -> the request mismatches the desired
            # state and REP_FAILURE is reported (state unchanged).
            if self.door_locked:
                return CommandOutcome(Rep.FAILURE, rep=True, publish_stat=True)
            self.door = Door.OPEN if cmd == Cmd.LOCK_OPEN else Door.CLOSED
            return CommandOutcome(Rep.SUCCESS, rep=True, publish_stat=True)
        if cmd in (Cmd.RELAY_ON, Cmd.RELAY_OFF, Cmd.RELAY_REBOOT):
            if self.version == 2 and self.relay_en == 0:
                return CommandOutcome(Rep.NOT_SUPPORTED, rep=True, publish_stat=False)
            target = com.data  # 0 = all, 1..4 = specific relay
            # C++ changingStatRelay: rel_number outside {0,1,2,3,4} -> REP_NOT_SUPPORTED,
            # no state change.
            if target not in (0, 1, 2, 3, 4):
                return CommandOutcome(Rep.NOT_SUPPORTED, rep=True, publish_stat=False)
            relays = [1, 2, 3, 4] if target == 0 else [target]
            result = Rep.SUCCESS
            for rel in relays:
                i = rel - 1
                # Locked relay: control checkbox untouched -> requested change does
                # not take effect -> REP_FAILURE for that relay (state unchanged).
                if rel in self.locked_relays:
                    result = Rep.FAILURE
                    continue
                if cmd == Cmd.RELAY_ON:
                    self.relays[i] = 1
                elif cmd == Cmd.RELAY_OFF:
                    self.relays[i] = 0
                else:  # REBOOT: C++ does off -> 1s pause -> on. The 1s OFF->ON pause
                    # is intentionally collapsed for the headless double (no
                    # behavioral test depends on the transient OFF window); end
                    # state is ON, matching the C++ reboot result.
                    self.relays[i] = 0
                    self.relays[i] = 1
            return CommandOutcome(result, rep=True, publish_stat=True)
        return CommandOutcome(Rep.NOT_SUPPORTED, rep=True, publish_stat=False)

    def build_stat(self) -> bytes:
        from .protocol import encode_stat_v1, encode_stat_v2
        if self.version == 1:
            return encode_stat_v1(self.device_id, self.relays, self.status, self.signal, self.alarm)
        if self.relay_en == 1:
            return encode_stat_v2(self.device_id, self.door, self.power, self.battery,
                                  self.net_type, 1, self.relays, self.status, self.signal)
        return encode_stat_v2(self.device_id, self.door, self.power, self.battery,
                              self.net_type, 0)
