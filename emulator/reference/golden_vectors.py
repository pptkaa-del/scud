"""Golden byte-vectors for the relay protocol, derived from the official doc and
the vendored protocol_structures.h. BOTH the emulator (scud_relay) and the
backend (locker_api.app.mqtt.relay_codec) are tested against these EXACT bytes,
guaranteeing byte-for-byte interop ('strictly verify against the original')."""


def u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


# com/{id}: cmd_id, command, data  (12 bytes)
COM_LOCK_OPEN = {"cmd_id": 7, "command": 5, "data": 0,
                 "bytes": u32(7) + u32(5) + u32(0)}
COM_RELAY_ON_2 = {"cmd_id": 1, "command": 1, "data": 2,
                  "bytes": u32(1) + u32(1) + u32(2)}

# rep: id, cmd_id, success  (12 bytes)
REP_SUCCESS = {"device_id": 1, "cmd_id": 7, "result": 1,
               "bytes": u32(1) + u32(7) + u32(1)}

# stat v1 (36 bytes): id, version=1, r1..r4, stat, sl, alarm
STAT_V1 = {"device_id": 1, "version": 1, "relays": [1, 0, 0, 0],
           "status": 1, "signal": 20, "alarm": 0,
           "bytes": u32(1) + u32(1) + u32(1) + u32(0) + u32(0) + u32(0)
                    + u32(1) + u32(20) + u32(0)}

# stat v2 relay_en=0 (28 bytes): id, version=2, door, power, battery, net, relay_en=0
STAT_V2_NORELAY = {"device_id": 1, "version": 2, "door": 1, "power": 1,
                   "battery": 85, "net_type": 1, "relay_en": 0,
                   "bytes": u32(1) + u32(2) + u32(1) + u32(1) + u32(85) + u32(1) + u32(0)}

# stat v2 relay_en=1 (52 bytes)
STAT_V2_RELAY = {"device_id": 1, "version": 2, "door": 2, "power": 0,
                 "battery": 50, "net_type": 2, "relay_en": 1, "relays": [1, 1, 0, 0],
                 "status": 1, "signal": 31,
                 "bytes": u32(1) + u32(2) + u32(2) + u32(0) + u32(50) + u32(2) + u32(1)
                          + u32(1) + u32(1) + u32(0) + u32(0) + u32(1) + u32(31)}

# photos frame (protocol §3.4): device_id, event_id, photos_cnt, photo_id, size, body.
# Single-frame series (photos_cnt=1, photo_id=1) — the shape relay --photos emits.
PHOTO_SINGLE = {"device_id": 1, "event_id": 7, "photos_cnt": 1, "photo_id": 1,
                "body": b"AB",
                "bytes": u32(1) + u32(7) + u32(1) + u32(1) + u32(2) + b"AB"}
