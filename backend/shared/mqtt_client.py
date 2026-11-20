"""Thin async wrapper over vendored gmqtt — normalizes to our small API.

gmqtt is vendored at shared/_vendor/gmqtt (no pip in the certified image). We add
that dir to sys.path so the package imports as top-level `gmqtt` regardless of any
internal absolute imports. Exposes: connect, add_subscription/subscribe, async
publish, set_message_handler, run_forever, disconnect. Resubscribes on (re)connect.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from enum import IntEnum
import logging
import os
import sys
from typing import Any

_VENDOR = os.path.join(os.path.dirname(__file__), "_vendor")
if _VENDOR not in sys.path:
    sys.path.insert(0, _VENDOR)

import gmqtt  # noqa: E402  (vendored; path inserted above)

logger = logging.getLogger(__name__)

MessageHandler = Callable[[str, bytes], Awaitable[None]]


class MqttQoS(IntEnum):
    AT_MOST_ONCE = 0
    AT_LEAST_ONCE = 1
    EXACTLY_ONCE = 2


# gmqtt async on_message must return a PUBACK reason code; 0 = success.
MQTT_ACK_OK = 0
CONNACK_ACCEPTED = 0


class MqttClient:
    def __init__(self, host: str, port: int, client_id: str,
                 username: str | None = None, password: str | None = None,
                 keepalive: int = 60, client_factory: Callable[[str], Any] | None = None) -> None:
        self.host = host
        self.port = port
        self.keepalive = keepalive
        # client_factory lets unit tests inject a fake without a real gmqtt/broker.
        # gmqtt is an untyped vendored package, so the client is typed Any.
        factory = client_factory if client_factory is not None else gmqtt.Client
        self._client: Any = factory(client_id)
        if username is not None:
            self._client.set_auth_credentials(username, password)
        self._handler: MessageHandler | None = None
        self._subs: dict[str, int] = {}  # topic -> qos (dedup)
        self._client.on_message = self._on_message
        self._client.on_connect = self._on_connect

    @property
    def is_connected(self) -> bool:
        """True если нижележащий gmqtt-клиент держит сессию с брокером.

        Делегирует в gmqtt.Client.is_connected. Возвращает False, если клиент
        ещё не создан/уже снят (self._client is None) или у фейка нет атрибута —
        зонд готовности (shared.auth.health.mqtt_check) и gauge
        scud_mqtt_connected не должны падать на ранней стадии запуска.
        """
        if self._client is None:
            return False
        return bool(getattr(self._client, "is_connected", False))

    def set_message_handler(self, handler: MessageHandler) -> None:
        self._handler = handler

    def add_subscription(self, topic: str, qos: int = MqttQoS.AT_LEAST_ONCE) -> None:
        self._subs[topic] = qos  # idempotent

    async def _on_message(self, _client: Any, topic: str, payload: bytes, _qos: int,
                          _properties: Any) -> int:
        # gmqtt-mandated callback signature; unused params are underscore-prefixed.
        # gmqtt invokes this callback via ensure_future and PUBACKs optimistically,
        # so a propagating exception would vanish into a detached task with the
        # message already acked. Catch and log (topic + length only, never the
        # payload bytes — they may be large/sensitive) so ingest failures stay
        # visible, then still return MQTT_ACK_OK since the ack can't be undone.
        if self._handler is not None:
            try:
                await self._handler(topic, bytes(payload))
            except Exception:
                logger.exception(
                    "MQTT message handler failed for topic %s (%d bytes); "
                    "message already acked",
                    topic, len(payload),
                )
        return MQTT_ACK_OK

    def _on_connect(self, client: Any, _flags: Any, rc: int, _properties: Any) -> None:
        if rc != CONNACK_ACCEPTED:
            logger.error("MQTT connect rejected rc=%s for %s:%s", rc, self.host, self.port)
            return
        for topic, qos in self._subs.items():
            client.subscribe(topic, qos=qos)
        logger.info("MQTT connected %s:%s (resubscribed %d)", self.host, self.port, len(self._subs))

    async def connect(self) -> None:
        await self._client.connect(self.host, self.port, keepalive=self.keepalive)

    async def subscribe(self, topic: str, qos: int = MqttQoS.AT_LEAST_ONCE) -> None:
        self.add_subscription(topic, qos)
        self._client.subscribe(topic, qos=qos)

    async def publish(self, topic: str, payload: bytes, qos: int = MqttQoS.AT_MOST_ONCE,
                      retain: bool = False) -> None:
        self._client.publish(topic, payload, qos=qos, retain=retain)

    async def disconnect(self) -> None:
        await self._client.disconnect()

    async def run_forever(self) -> None:
        await self.connect()
        # gmqtt runs its own reader + auto-reconnect; stay alive until cancelled,
        # then disconnect cleanly so the broker session isn't leaked.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.disconnect()
            raise
