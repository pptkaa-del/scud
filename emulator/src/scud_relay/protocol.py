"""Device-side binary relay codec (mirror of backend relay_codec, opposite
direction). uint32 little-endian. Verified byte-identical via golden_vectors."""
from __future__ import annotations

from dataclasses import dataclass


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def _r32(data: bytes, off: int) -> int:
    return int.from_bytes(data[off:off + 4], "little")


@dataclass(slots=True)
class Com:
    cmd_id: int
    command: int
    data: int


def decode_com(raw: bytes) -> Com:
    if len(raw) != 12:
        raise ValueError(f"com must be 12 bytes, got {len(raw)}")
    return Com(cmd_id=_r32(raw, 0), command=_r32(raw, 4), data=_r32(raw, 8))


def encode_rep(device_id: int, cmd_id: int, result: int) -> bytes:
    return _u32(device_id) + _u32(cmd_id) + _u32(result)


def encode_stat_v1(device_id: int, relays: list[int], status: int, signal: int,
                   alarm: int) -> bytes:
    body = _u32(device_id) + _u32(1)
    for r in relays:
        body += _u32(r)
    body += _u32(status) + _u32(signal) + _u32(alarm)
    return body  # 36 bytes


def encode_stat_v2(device_id: int, door: int, power: int, battery: int, net_type: int,
                   relay_en: int, relays: list[int] | None = None,
                   status: int | None = None, signal: int | None = None) -> bytes:
    body = _u32(device_id) + _u32(2) + _u32(door) + _u32(power) + _u32(battery) \
        + _u32(net_type) + _u32(relay_en)
    if relay_en == 1:
        assert relays is not None and status is not None and signal is not None
        for r in relays:
            body += _u32(r)
        body += _u32(status) + _u32(signal)
    return body  # 28 or 52 bytes


def encode_photo(device_id: int, event_id: int, photos_cnt: int, photo_id: int,
                 body: bytes) -> bytes:
    """Encode one `photos` frame (protocol §3.4): 20-byte header + body.

    Header is five uint32 LE fields — device_id, event_id, photos_cnt, photo_id,
    photo_size — followed by ``photo_size`` body bytes. Byte-for-byte inverse of the
    backend ``relay_codec.decode_photo``; a series is complete once frames 1..photos_cnt
    for one (device_id, event_id) arrive.
    """
    return (_u32(device_id) + _u32(event_id) + _u32(photos_cnt)
            + _u32(photo_id) + _u32(len(body)) + body)
