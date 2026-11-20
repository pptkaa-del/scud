"""MQTT transport for a headless relay device, over gmqtt.

Connects with the firmware's clientId/username/password (protocol §4), subscribes
com/{id}, publishes stat/rep. The emulator is a development/test/load tool outside
Astra certification, so gmqtt is a normal pip dependency here (the backend, which is
certification-constrained, vendors its own copy instead).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import random
import secrets
import ssl
from typing import Any

import gmqtt

from .device import RelayDevice
from .protocol import decode_com, encode_photo, encode_rep
from .scenario import Incident, SecurityEventScenario
from .tamper_photo import TAMPER_JPEG

logger = logging.getLogger(__name__)

_MQTT_ACK_OK = 0


def broker_username(relay_id: int) -> str:
    return f"relay-{relay_id}"


def broker_password(relay_id: int) -> str:
    return hashlib.sha256(broker_username(relay_id).encode("ascii")).digest()[:16].hex()


def client_id(relay_id: int, version: int) -> str:
    return f"relay-id{relay_id}-v{version}"


DEFAULT_PHOTOS_PER_INCIDENT = 5
MAX_PHOTOS_PER_INCIDENT = 16
_JPEG_SOI = b"\xff\xd8"
_JPEG_COM = b"\xff\xfe"


def _tamper_jpeg_variant(tag: str) -> bytes:
    """TAMPER_JPEG with a COM segment right after SOI, plus board-style zero padding.

    The comment makes every frame a distinct, still renderable JPEG; the padding after
    EOI mimics the board's fixed-size buffer and must not affect backend dedup.
    """
    comment = tag.encode("ascii")
    segment = _JPEG_COM + (len(comment) + 2).to_bytes(2, "big") + comment
    padding = b"\x00" * random.randint(64, 256)
    return _JPEG_SOI + segment + TAMPER_JPEG[len(_JPEG_SOI):] + padding


def build_tamper_photo_series(device_id: int, event_id: int,
                              count: int = DEFAULT_PHOTOS_PER_INCIDENT) -> list[bytes]:
    """`count` photos frames of one incident: photo_id 1..count, each its own JPEG."""
    if not 1 <= count <= MAX_PHOTOS_PER_INCIDENT:
        raise ValueError(f"count must be 1..{MAX_PHOTOS_PER_INCIDENT}, got {count}")
    return [
        encode_photo(device_id, event_id, photos_cnt=count, photo_id=i,
                     body=_tamper_jpeg_variant(
                         f"{device_id}:{event_id}:{i}:{secrets.token_hex(4)}"))
        for i in range(1, count + 1)
    ]


_DEFAULT_STATUS_INTERVAL_S = 300  # C++ statusTimer maxStatusIntervalMs (300s heartbeat)
_DEFAULT_EVENTS_INTERVAL_S = 30.0  # gap between security-event scenario steps


def build_tls_context(cafile: str | None = None, *, insecure: bool = False) -> ssl.SSLContext:
    """TLS 1.2+ client context for the 8883 listener (protocol §4).

    The stand terminates TLS with a private CA, so `cafile` is normally required;
    `insecure` skips verification for a quick reachability probe only.
    """
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    if insecure:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    elif cafile:
        ctx.load_verify_locations(cafile)
    else:
        ctx.load_default_certs(ssl.Purpose.SERVER_AUTH)
    return ctx


class RelayMqttTransport:
    # Значение по умолчанию на уровне класса: тесты собирают транспорт через
    # __new__, минуя __init__, и должны получать общую схему топиков.
    _per_id_topics = False

    def __init__(self, device: RelayDevice, host: str, port: int = 1883,
                 keepalive: int = 95,
                 status_interval_s: float = _DEFAULT_STATUS_INTERVAL_S,
                 events: bool = False,
                 events_interval_s: float = _DEFAULT_EVENTS_INTERVAL_S,
                 photos: bool = False,
                 tls_context: ssl.SSLContext | None = None,
                 per_id_topics: bool = False,
                 photos_per_incident: int = DEFAULT_PHOTOS_PER_INCIDENT) -> None:
        self._dev = device
        self._host = host
        self._port = port
        self._keepalive = keepalive
        self._tls_context = tls_context
        # ПИВ v2 публикует в `<kind>/{id}`; прошивки прежней ревизии — в общий
        # топик. Бэкенд принимает обе схемы, флаг выбирает проверяемую.
        self._per_id_topics = per_id_topics
        self._status_interval_s = status_interval_s
        # Optional security-event scenario: when enabled, the device cycles through
        # stat mutations (power-loss / ERROR / offline) so the backend emits
        # device-asserted security events on a fixed interval.
        self._events_interval_s = events_interval_s
        self._scenario = SecurityEventScenario(device) if events else None
        # Optional tamper-photo emission: on each event-emitting incident the device
        # also publishes a `photos` series (photos_per_incident distinct JPEG frames),
        # so the backend records one image/jpeg per frame tied to the incident.
        # Photos ride the error incidents, so they are meaningless without the scenario.
        self._photos = photos and events
        self._photos_per_incident = photos_per_incident
        self._photo_salt = random.getrandbits(15)  # per-instance high bits of event_id
        self._photo_seq = 0
        rid = device.device_id
        self._client: Any = gmqtt.Client(client_id(rid, device.version))
        self._client.set_auth_credentials(broker_username(rid), broker_password(rid))
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _topic(self, kind: str) -> str:
        return f"{kind}/{self._dev.device_id}" if self._per_id_topics else kind

    def _on_connect(self, client: Any, _flags: Any, _rc: int, _properties: Any) -> None:
        rid = self._dev.device_id
        client.subscribe(f"com/{rid}", qos=1)
        # initial retained stat on connect (protocol §4)
        client.publish(self._topic("stat"), self._dev.build_stat(), qos=1, retain=True)

    async def _on_message(self, client: Any, _topic: str, payload: bytes, _qos: int,
                          _properties: Any) -> int:
        rid = self._dev.device_id
        raw = bytes(payload)
        try:
            com = decode_com(raw)
            outcome = self._dev.apply_command(com)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "device %s: malformed com frame ignored (%d bytes, %s): %s",
                rid, len(raw), raw[:4].hex() if raw else "<empty>", exc,
            )
            return _MQTT_ACK_OK
        if outcome.rep:
            client.publish(self._topic("rep"), encode_rep(rid, com.cmd_id, outcome.result),
                           qos=2, retain=False)
        if outcome.publish_stat:
            client.publish(self._topic("stat"), self._dev.build_stat(), qos=1, retain=True)
        return _MQTT_ACK_OK

    def _next_heartbeat_delay(self) -> float:
        """Интервал до следующего heartbeat с ±15% джиттером.

        Без джиттера 1000 синхронно стартовавших плат шлют heartbeat-stat
        одновременно каждые status_interval_s — thundering herd, который кладёт
        ingest (DB-executor wedge). Джиттер десинхронизирует поток и держит
        десинхрон даже без ramp-up на старте флота."""
        return self._status_interval_s * random.uniform(0.85, 1.15)

    async def _heartbeat(self) -> None:
        """Publish a periodic retained stat (firmware statusTimer, protocol §5).

        Keeps the device's online state fresh for the backend offline-monitor even
        when nothing changes. Cancellation-safe: a CancelledError simply ends the loop.
        """
        try:
            while True:
                await asyncio.sleep(self._next_heartbeat_delay())
                self._client.publish(self._topic("stat"), self._dev.build_stat(), qos=1, retain=True)
        except asyncio.CancelledError:
            raise

    def _next_photo_event_id(self) -> int:
        """A per-run-unique uint32 event_id: 15 random high bits + a monotonic 16-bit
        sequence. The backend keys a series by (relay, event_id) within its epoch, so a
        fresh salt per run keeps reruns from reusing event_ids; an accidental reuse is
        still detected backend-side (distinct frame bytes reset the series)."""
        self._photo_seq = (self._photo_seq + 1) & 0xFFFF
        return (self._photo_salt << 16) | self._photo_seq

    def _emit_scenario_step(self, incident: Incident) -> None:
        """Publish the stat after a scenario mutation, and — when photos are enabled and
        the incident raises a backend event — its photo series, frames in photo_id order."""
        self._client.publish(self._topic("stat"), self._dev.build_stat(), qos=1, retain=True)
        if self._photos and incident.emits_event:
            frames = build_tamper_photo_series(
                self._dev.device_id, self._next_photo_event_id(), self._photos_per_incident)
            for frame in frames:
                self._client.publish(self._topic("photos"), frame, qos=1, retain=False)

    async def _event_loop(self) -> None:
        """Advance the security-event scenario one step per interval, publishing a stat
        (and, with --photos, a tamper photo on each error incident) after each mutation.
        Cancellation-safe: a CancelledError ends the loop."""
        scenario = self._scenario
        assert scenario is not None  # only started when events enabled
        rid = self._dev.device_id
        try:
            while True:
                await asyncio.sleep(self._events_interval_s * random.uniform(0.85, 1.15))
                incident = scenario.step()
                self._emit_scenario_step(incident)
                logger.info("device %s: security-event step '%s'%s%s", rid,
                            incident.label,
                            " → backend emits event" if incident.emits_event else "",
                            " +photos" if (self._photos and incident.emits_event) else "")
        except asyncio.CancelledError:
            raise

    async def run(self) -> None:
        """Connect and stay alive until cancelled (gmqtt runs its own reader)."""
        await self._client.connect(self._host, self._port, keepalive=self._keepalive,
                                   ssl=self._tls_context or False)
        tasks = [asyncio.ensure_future(self._heartbeat())]
        if self._scenario is not None:
            tasks.append(asyncio.ensure_future(self._event_loop()))
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            await self._client.disconnect()
            raise
