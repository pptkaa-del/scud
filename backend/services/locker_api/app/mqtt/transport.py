"""Transport abstraction decoupling CommandQueue from the wire protocol.

MqttTransport publishes binary com/{relay_id}. CommandQueue stores
DeviceTransport.
"""
from __future__ import annotations

import logging
from typing import Any, Protocol

from shared.mqtt_client import MqttQoS

from .relay_codec import command_type_to_relay, encode_com
from .relay_protocol import Command, topic_com

logger = logging.getLogger(__name__)

# The single retained com/{id} slot belongs to the lock: a reconnecting board
# re-reads the desired lock state from it. Relay and one-shot commands are not
# retained, otherwise a relay command would overwrite the lock state and a
# stale reboot would re-execute on reconnect.
_RETAINED_COMMANDS = frozenset({
    Command.LOCK_OPEN,
    Command.LOCK_CLOSE,
})


class RelayCmdIdAllocator(Protocol):
    """Минимальный порт для выделения uint32 relay_cmd_id.

    MqttTransport.prepare_send зависит ТОЛЬКО от этого метода, а не от всего
    CommandRepository — транспортному слою не нужен широкий persistence-доступ.
    CommandRepository удовлетворяет протоколу структурно, поэтому ingest
    передаёт тот же объект, типизированный как этот порт.
    """

    async def allocate_relay_cmd_id(self, command_id: str, locker_id: str) -> int: ...


class DeviceTransport(Protocol):
    locker_id: str

    async def prepare_send(self, command: Any, command_data: dict[str, Any]) -> None:
        """Транспорт-специфичная подготовка command_data ПЕРЕД send_command.

        Owns любой pre-send шаг, зависящий от протокола: WS — no-op, MQTT —
        выделение uint32 relay_cmd_id. CommandQueue зовёт это перед send_command
        в обоих путях (queue-processor и inline), не зная о конкретном транспорте.
        """
        ...

    async def send_command(self, command_data: dict[str, Any]) -> bool: ...


class MqttTransport:
    """Publishes binary commands to com/{relay_id} (QoS1). Retain flag depends on
    the command kind: state commands are retained for reconnect-time restoration,
    one-shot/query commands are not."""

    def __init__(self, locker_id: str, relay_id: int, client: Any,
                 allocator: RelayCmdIdAllocator | None = None) -> None:
        self.locker_id = locker_id
        self.relay_id = relay_id
        self._client = client
        # Узкий порт для выделения uint32 relay_cmd_id в prepare_send (а не весь
        # CommandRepository). Инжектится в ingest при построении транспорта. None
        # в части unit-тестов, которые проверяют только send_command (relay_cmd_id
        # кладут вручную).
        self._allocator = allocator

    async def prepare_send(self, command: Any, command_data: dict[str, Any]) -> None:
        """Выделить uint32 relay_cmd_id и положить его в command_data.

        relay_cmd_id отправляется плате в com/{id} и эхом возвращается в rep
        (locker_commands.id — UUID, int(uuid) недопустим). Идемпотентно: если у
        команды уже есть relay_cmd_id (повтор после ack-timeout), переиспользуем
        его, не дёргая аллокатор — плата ждёт тот же id в rep.
        """
        if self._allocator is None:
            return
        existing = getattr(command, "relay_cmd_id", None)
        if existing is not None:
            command_data["relay_cmd_id"] = int(existing)
            return
        command_data["relay_cmd_id"] = await self._allocator.allocate_relay_cmd_id(
            command.id, command.locker_id,
        )

    async def send_command(self, command_data: dict[str, Any]) -> bool:
        # gmqtt не бросает при оборванной сессии с брокером, а копит QoS1-пакет
        # для переотправки: без этой проверки команда стала бы `sent`, не уйдя.
        if not self._client.is_connected:
            logger.warning("MQTT broker session down, command not sent to locker %s",
                           self.locker_id)
            return False
        try:
            command = command_type_to_relay(command_data["command_type"])
            # relay_cmd_id is the uint32 allocated by the queue processor,
            # NOT int(command_id) — locker_commands.id is a UUID.
            cmd_id = int(command_data["relay_cmd_id"]) & 0xFFFFFFFF
            data = int(command_data.get("parameters", {}).get("relay", 0))
            payload = encode_com(cmd_id=cmd_id, command=command, data=data)
            retain = command in _RETAINED_COMMANDS
            await self._client.publish(topic_com(self.relay_id), payload,
                                       qos=MqttQoS.AT_LEAST_ONCE, retain=retain)
            return True
        except Exception as exc:
            logger.error("MQTT send failed for locker %s: %s", self.locker_id, exc)
            return False
