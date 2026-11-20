# services/locker_api/app/mqtt/relay_codec.py
"""Binary relay protocol codec (uint32 little-endian). Pure functions, no I/O.

Length-tolerant `stat` decoding (see decode_stat): real boards and the reference
emulator send a fixed 52-byte v2 frame; a 28-byte relay_en=0 frame is accepted too.
"""
from __future__ import annotations

from dataclasses import dataclass
import os

from ..locker_data.db.models.command import CommandType
from .relay_protocol import (
    REP_LEN,
    STAT_V1_LEN,
    STAT_V2_NORELAY_LEN,
    STAT_V2_RELAY_LEN,
    U32,
    Command,
    DeviceStatus,
    DoorStateRaw,
    PowerSource,
    ProtocolVersion,
    RepResult,
)

# Верхняя граница тела MQTT photo-кадра (SEC-04). gmqtt ограничивает лишь
# протокольный максимум (256 МиБ), а MQTT-путь фото — в отличие от WS-пути —
# не имел собственного предела: relay мог объявить огромный size и заставить
# ingest-таск аллоцировать кадр того же размера (memory-pressure DoS). Выровнено
# с WS-лимитом медиапередачи MAX_MEDIA_TRANSFER_BYTES (device_handler.py),
# default 10 МиБ; переопределяется тем же env, чтобы оба пути двигались синхронно.
MAX_RELAY_PHOTO_BYTES = int(os.getenv("MAX_MEDIA_TRANSFER_BYTES", str(10 * 1024 * 1024)))


def _u32(data: bytes, offset: int) -> int:
    return int.from_bytes(data[offset:offset + U32], "little")


def encode_com(cmd_id: int, command: Command, data: int = 0) -> bytes:
    return (
        cmd_id.to_bytes(U32, "little")
        + int(command).to_bytes(U32, "little")
        + data.to_bytes(U32, "little")
    )


@dataclass(slots=True)
class Rep:
    device_id: int
    cmd_id: int
    result: RepResult


def decode_rep(raw: bytes) -> Rep:
    if len(raw) != REP_LEN:
        raise ValueError(f"rep must be {REP_LEN} bytes, got {len(raw)}")
    return Rep(device_id=_u32(raw, 0), cmd_id=_u32(raw, 4), result=RepResult(_u32(raw, 8)))


@dataclass(slots=True)
class Stat:
    device_id: int
    version: int
    status: DeviceStatus | None      # None for a v2 relay_en=0 frame shorter than 52 bytes
    signal: int | None
    relays: list[int] | None         # 4 relay states; None when absent
    door: DoorStateRaw | None        # v2 only
    power: PowerSource | None        # v2 only
    battery: int | None              # v2 only
    net_type: int | None             # v2 only
    relay_en: int | None             # v2 only
    alarm: int | None                # v1 only


def decode_stat(raw: bytes) -> Stat:
    if len(raw) < 2 * U32:  # need at least id + version
        raise ValueError(f"stat too short for header: {len(raw)} bytes")
    device_id = _u32(raw, 0)
    version = _u32(raw, 4)

    if version == ProtocolVersion.V1:
        if len(raw) < STAT_V1_LEN:
            raise ValueError(f"v1 stat must be >= {STAT_V1_LEN} bytes, got {len(raw)}")
        return Stat(
            device_id=device_id, version=version,
            relays=[_u32(raw, 8), _u32(raw, 12), _u32(raw, 16), _u32(raw, 20)],
            status=DeviceStatus(_u32(raw, 24)), signal=_u32(raw, 28),
            alarm=_u32(raw, 32),
            door=None, power=None, battery=None, net_type=None, relay_en=None,
        )

    if version == ProtocolVersion.V2:
        if len(raw) < STAT_V2_NORELAY_LEN:
            raise ValueError(f"v2 stat must be >= {STAT_V2_NORELAY_LEN} bytes, got {len(raw)}")
        relay_en = _u32(raw, 24)
        if relay_en == 1:
            if len(raw) < STAT_V2_RELAY_LEN:
                raise ValueError(
                    f"v2 relay stat must be >= {STAT_V2_RELAY_LEN} bytes, got {len(raw)}")
            return Stat(
                device_id=device_id, version=version,
                door=DoorStateRaw(_u32(raw, 8)), power=PowerSource(_u32(raw, 12)),
                battery=_u32(raw, 16), net_type=_u32(raw, 20), relay_en=relay_en,
                relays=[_u32(raw, 28), _u32(raw, 32), _u32(raw, 36), _u32(raw, 40)],
                status=DeviceStatus(_u32(raw, 44)), signal=_u32(raw, 48),
                alarm=None,
            )
        # relay_en == 0: r1..r4 бессмысленны (плата шлёт 2, эталонный эмулятор — 0),
        # но stat/sl реальная плата и эмулятор заполняют всегда. 28-байтовый кадр
        # из буквы ПИВ («конец сообщения») тоже принимаем — тогда stat/sl нет.
        has_tail = len(raw) >= STAT_V2_RELAY_LEN
        return Stat(
            device_id=device_id, version=version,
            door=DoorStateRaw(_u32(raw, 8)), power=PowerSource(_u32(raw, 12)),
            battery=_u32(raw, 16), net_type=_u32(raw, 20), relay_en=relay_en,
            relays=None,
            status=DeviceStatus(_u32(raw, 44)) if has_tail else None,
            signal=_u32(raw, 48) if has_tail else None,
            alarm=None,
        )

    raise ValueError(f"unknown stat version: {version}")


_COMMAND_TYPE_MAP = {
    CommandType.OPEN.value: Command.LOCK_OPEN,
    CommandType.CLOSE.value: Command.LOCK_CLOSE,
    CommandType.REBOOT.value: Command.RELAY_REBOOT,
    CommandType.RELAY_ON.value: Command.RELAY_ON,
    CommandType.RELAY_OFF.value: Command.RELAY_OFF,
    CommandType.RELAY_RESTART.value: Command.RELAY_REBOOT,
}


def command_type_to_relay(command_type: str) -> Command:
    try:
        return _COMMAND_TYPE_MAP[command_type]
    except KeyError:
        raise ValueError(f"no relay mapping for command_type={command_type!r}") from None


@dataclass(slots=True)
class Photo:
    device_id: int
    event_id: int
    photos_cnt: int
    photo_id: int
    photo_size: int
    photo: bytes


def decode_photo(raw: bytes) -> Photo:
    if len(raw) < 5 * U32:
        raise ValueError(f"photos header needs >= {5 * U32} bytes, got {len(raw)}")
    size = _u32(raw, 16)
    # SEC-04: отвергаем по объявленному size ДО slice — не аллоцируем огромный
    # кадр на forged/buggy relay-фрейме (decode-bomb guard).
    if size > MAX_RELAY_PHOTO_BYTES:
        raise ValueError(f"photo size {size} exceeds limit {MAX_RELAY_PHOTO_BYTES}")
    # Bounds-check ДО slice: raw короче 20+size → malformed (без IndexError и
    # без молчаливого усечения тела).
    if len(raw) < 20 + size:
        raise ValueError(f"photo body truncated: have {len(raw) - 20}, declared {size}")
    photo = raw[20:20 + size]
    return Photo(_u32(raw, 0), _u32(raw, 4), _u32(raw, 8), _u32(raw, 12), size, photo)
