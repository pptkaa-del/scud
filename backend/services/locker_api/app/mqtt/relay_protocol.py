# services/locker_api/app/mqtt/relay_protocol.py
"""Relay board MQTT protocol constants (binary, uint32 LE).

Source of truth: official protocol doc. Discriminator is the `version` field at
offset 4 of `stat`: 1 = v1, 2 = v2. VERIFIED against RelayEmulator dev_v2
emulator.cpp:503 (`pkt.version = protocolVersion`, PROTOCOL_V1=1/PROTOCOL_V2=2);
the v2 table's "=1" in the doc is a confirmed copy-paste typo.
"""
from __future__ import annotations

from enum import IntEnum

# Topics
TOPIC_STAT = "stat"
TOPIC_REP = "rep"
TOPIC_PHOTOS = "photos"


def topic_com(relay_id: int) -> str:
    return f"com/{relay_id}"


# Платы разных ревизий публикуют либо в общий топик, либо в `<kind>/{id}`
# (ПИВ v2). Принимаем обе схемы: суффикс, если он есть, служит проверкой —
# идентификатор в нём обязан совпасть с id внутри кадра.
_DEVICE_TOPICS_ORDERED = (TOPIC_STAT, TOPIC_REP, TOPIC_PHOTOS)
_DEVICE_TOPICS = frozenset(_DEVICE_TOPICS_ORDERED)


def parse_device_topic(topic: str) -> tuple[str, int | None] | None:
    """Разбирает топик публикации устройства в (вид, relay_id|None).

    Возвращает None для всего, что устройство публиковать не может.
    """
    kind, sep, suffix = topic.partition("/")
    if kind not in _DEVICE_TOPICS:
        return None
    if not sep:
        return kind, None
    # Только один уровень и только целое без ведущих нулей (см. ПИВ §2).
    if not suffix.isdigit() or (len(suffix) > 1 and suffix[0] == "0"):
        return None
    return kind, int(suffix)


def device_subscriptions() -> tuple[str, ...]:
    """Фильтры подписки под обе схемы публикации.

    Одноуровневый `+`, не `#`: `stat/#` тянул бы произвольную вложенность.
    """
    return tuple(_DEVICE_TOPICS_ORDERED) + tuple(f"{t}/+" for t in _DEVICE_TOPICS_ORDERED)


def is_rep_topic(topic: str) -> bool:
    """rep освобождён от сброса в bulkhead — это ответы на команды."""
    parsed = parse_device_topic(topic)
    return parsed is not None and parsed[0] == TOPIC_REP


def is_photos_topic(topic: str) -> bool:
    """photos идут отдельной полосой bulkhead с бюджетом байт."""
    parsed = parse_device_topic(topic)
    return parsed is not None and parsed[0] == TOPIC_PHOTOS


class Command(IntEnum):
    NONE = 0
    RELAY_ON = 1
    RELAY_OFF = 2
    RELAY_REBOOT = 3
    SEND_STATUS = 4
    LOCK_OPEN = 5   # v2
    LOCK_CLOSE = 6  # v2


class RepResult(IntEnum):
    SUCCESS = 1
    FAILURE = 2
    NOT_SUPPORTED = 3


class DeviceStatus(IntEnum):
    OFFLINE = 0
    ONLINE = 1
    ERROR = 2


class DoorStateRaw(IntEnum):
    UNDEFINED = 0
    OPEN = 1
    CLOSED = 2


class PowerSource(IntEnum):
    BATTERY = 0
    MAINS = 1


class NetType(IntEnum):
    UNDEFINED = 0
    ETHERNET = 1
    GSM = 2


class ProtocolVersion(IntEnum):
    V1 = 1
    V2 = 2


REP_LEN = 12
STAT_V1_LEN = 36
STAT_V2_NORELAY_LEN = 28
STAT_V2_RELAY_LEN = 52
U32 = 4
