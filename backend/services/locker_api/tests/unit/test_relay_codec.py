import pytest

from app.mqtt import relay_codec as rc
from app.mqtt.relay_protocol import (
    Command,
    DeviceStatus,
    DoorStateRaw,
    PowerSource,
    RepResult,
)


def test_encode_com_lock_open():
    # cmd_id=7, command=LOCK_OPEN(5), data=0
    assert rc.encode_com(cmd_id=7, command=Command.LOCK_OPEN, data=0) == bytes([
        0x07, 0, 0, 0,  0x05, 0, 0, 0,  0x00, 0, 0, 0,
    ])


def test_encode_com_relay_on_relay2():
    assert rc.encode_com(cmd_id=1, command=Command.RELAY_ON, data=2) == bytes([
        0x01, 0, 0, 0,  0x01, 0, 0, 0,  0x02, 0, 0, 0,
    ])


def test_decode_rep():
    raw = bytes([0x01, 0, 0, 0,  0x07, 0, 0, 0,  0x01, 0, 0, 0])
    rep = rc.decode_rep(raw)
    assert rep.device_id == 1
    assert rep.cmd_id == 7
    assert rep.result == RepResult.SUCCESS


def test_decode_rep_rejects_wrong_length():
    with pytest.raises(ValueError):
        rc.decode_rep(b"\x00" * 8)


def _u32le(n: int) -> bytes:
    return n.to_bytes(4, "little")


def test_decode_stat_v1_36_bytes():
    raw = (_u32le(1) + _u32le(1)            # id, version=1
           + _u32le(1) + _u32le(0) + _u32le(0) + _u32le(0)  # r1..r4
           + _u32le(1) + _u32le(20) + _u32le(0))            # stat, sl, alarm
    assert len(raw) == 36
    st = rc.decode_stat(raw)
    assert st.device_id == 1
    assert st.version == 1
    assert st.relays == [1, 0, 0, 0]
    assert st.status == DeviceStatus.ONLINE
    assert st.signal == 20
    assert st.door is None  # v1 has no door/battery
    assert st.battery is None


def test_decode_stat_v2_norelay_28_bytes():
    raw = (_u32le(1) + _u32le(2)            # id, version=2
           + _u32le(1) + _u32le(1) + _u32le(85) + _u32le(1) + _u32le(0))  # door,power,batt,net,relay_en=0
    assert len(raw) == 28
    st = rc.decode_stat(raw)
    assert st.version == 2
    assert st.door == DoorStateRaw.OPEN
    assert st.power == PowerSource.MAINS
    assert st.battery == 85
    assert st.relays is None     # relay block absent
    assert st.status is None     # stat field absent when relay_en=0


def test_decode_stat_v2_relay_52_bytes():
    raw = (_u32le(1) + _u32le(2)
           + _u32le(2) + _u32le(0) + _u32le(50) + _u32le(2) + _u32le(1)   # door=closed,power=batt,batt=50,net=gsm,relay_en=1
           + _u32le(1) + _u32le(1) + _u32le(0) + _u32le(0)                # r1..r4
           + _u32le(1) + _u32le(31))                                      # stat=online, sl=31
    assert len(raw) == 52
    st = rc.decode_stat(raw)
    assert st.door == DoorStateRaw.CLOSED
    assert st.relays == [1, 1, 0, 0]
    assert st.status == DeviceStatus.ONLINE
    assert st.signal == 31


def test_decode_stat_rejects_truncated_v2_relay_frame():
    # version=2, relay_en=1 but only 40 bytes (claims relay block, truncated) -> reject,
    # do NOT silently decode zeros for status/signal.
    def u32(n): return n.to_bytes(4, "little")
    raw = (u32(1) + u32(2) + u32(2) + u32(0) + u32(50) + u32(2) + u32(1)
           + u32(0) + u32(0) + u32(0))  # 40 bytes, missing r4/stat/sl
    with pytest.raises(ValueError):
        rc.decode_stat(raw)


def test_decode_stat_v2_relay_en_0_real_board_frame_reads_stat_and_signal():
    # Retained stat платы 1002 со стенда: relay_en=0, но кадр 52 байта —
    # r1..r4=2 (реле нет), stat=1, sl=0.
    raw = bytes.fromhex(
        "ea030000020000000200000001000000000000000100000000000000"
        "02000000020000000200000002000000"
        "0100000000000000")
    assert len(raw) == 52
    st = rc.decode_stat(raw)
    assert st.device_id == 1002
    assert st.relay_en == 0
    assert st.net_type == 1
    assert st.relays is None
    assert st.status == DeviceStatus.ONLINE
    assert st.signal == 0


def test_decode_stat_v2_relay_en_0_reference_emulator_frame():
    # RelayEmulator dev_v2 buildStatPayload: при relay_en=0 обнуляет только r1..r4.
    raw = (_u32le(1) + _u32le(2)
           + _u32le(1) + _u32le(1) + _u32le(90) + _u32le(2) + _u32le(0)   # relay_en=0
           + _u32le(0) * 4
           + _u32le(2) + _u32le(17))                                      # stat=error, sl=17
    st = rc.decode_stat(raw)
    assert st.relays is None
    assert st.status == DeviceStatus.ERROR
    assert st.signal == 17
    assert st.battery == 90


def test_decode_stat_v2_relay_en_0_partial_tail_is_ignored():
    raw = (_u32le(1) + _u32le(2)
           + _u32le(1) + _u32le(1) + _u32le(90) + _u32le(1) + _u32le(0)
           + _u32le(0) * 4)
    assert len(raw) == 44
    st = rc.decode_stat(raw)
    assert st.status is None
    assert st.signal is None


def test_command_type_to_relay():
    assert rc.command_type_to_relay("open") == Command.LOCK_OPEN
    assert rc.command_type_to_relay("close") == Command.LOCK_CLOSE
    assert rc.command_type_to_relay("reboot") == Command.RELAY_REBOOT
    assert rc.command_type_to_relay("relay_on") == Command.RELAY_ON
    assert rc.command_type_to_relay("relay_off") == Command.RELAY_OFF
    assert rc.command_type_to_relay("relay_restart") == Command.RELAY_REBOOT
    with pytest.raises(ValueError):
        rc.command_type_to_relay("nonsense")


def test_decode_photo_header():
    def u32(n): return n.to_bytes(4, "little")
    jpeg = b"\xff\xd8\xff\xe0body"
    raw = u32(5) + u32(42) + u32(3) + u32(1) + u32(len(jpeg)) + jpeg
    p = rc.decode_photo(raw)
    assert p.device_id == 5
    assert p.event_id == 42
    assert p.photos_cnt == 3
    assert p.photo_id == 1
    assert p.photo_size == len(jpeg)
    assert p.photo == jpeg


def test_decode_photo_rejects_oversized_declared_size():
    # SEC-04: declared size above MAX_RELAY_PHOTO_BYTES → reject BEFORE allocating
    # the slice (decode-bomb / memory-pressure DoS guard). No actual body needed:
    # the declared size alone must be rejected.
    def u32(n): return n.to_bytes(4, "little")
    oversize = rc.MAX_RELAY_PHOTO_BYTES + 1
    raw = u32(5) + u32(42) + u32(3) + u32(1) + u32(oversize)
    with pytest.raises(ValueError):
        rc.decode_photo(raw)


def test_decode_photo_rejects_short_body():
    # raw shorter than 20 + declared size → reject (no IndexError, no silent
    # truncation). Declares 100 bytes of body but supplies only 4.
    def u32(n): return n.to_bytes(4, "little")
    raw = u32(5) + u32(42) + u32(3) + u32(1) + u32(100) + b"abcd"
    with pytest.raises(ValueError):
        rc.decode_photo(raw)


def test_decode_photo_rejects_consistent_oversized_frame(monkeypatch):
    # SEC-04 (regression-fixating): a CONSISTENT oversized frame — declared
    # size == actual body length, just one byte over the cap. The OLD codec
    # (`photo = raw[20:20+size]; if len(photo) != size: raise`) had no size cap
    # and its length check PASSED here (len(photo) == size), so it ACCEPTED and
    # allocated the oversized photo. The new size-cap branch (`size >
    # MAX_RELAY_PHOTO_BYTES`) must REJECT before the slice. This test targets
    # the size-cap branch specifically (body is fully present, so the truncation
    # branch cannot fire) and FAILS against the pre-a386010 codec.
    # Cap is lowered so we build a small (consistent) frame instead of ~10 MiB.
    monkeypatch.setattr(rc, "MAX_RELAY_PHOTO_BYTES", 16)
    def u32(n): return n.to_bytes(4, "little")
    oversize = rc.MAX_RELAY_PHOTO_BYTES + 1  # = 17, just over the cap
    body = b"\x01" * oversize                # body length EXACTLY == declared size
    raw = u32(5) + u32(42) + u32(3) + u32(1) + u32(oversize) + body
    assert len(raw) == 20 + oversize         # frame is consistent, not truncated
    with pytest.raises(ValueError):
        rc.decode_photo(raw)


def test_decode_photo_accepts_body_at_limit():
    # Exactly at the bound must still decode (boundary is inclusive).
    def u32(n): return n.to_bytes(4, "little")
    body = b"\x00" * rc.MAX_RELAY_PHOTO_BYTES
    raw = u32(5) + u32(42) + u32(1) + u32(1) + u32(len(body)) + body
    p = rc.decode_photo(raw)
    assert p.photo_size == rc.MAX_RELAY_PHOTO_BYTES
    assert len(p.photo) == rc.MAX_RELAY_PHOTO_BYTES
