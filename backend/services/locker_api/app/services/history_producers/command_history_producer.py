"""Продьюсер истории команд в history_api.

Жизненный цикл команды → переходы статусов:
  pending  → publish_created  (POST команды через LockerCommandHandler)
  sent     → publish_sent     (after update_status_to_sent в device_handler)
  executed → publish_executed (after update_status_to_executed)
  failed   → publish_failed   (after update_status_to_failed)

Эндпойнт history_api идемпотентен по `command_id`, поэтому каждый переход
безопасно публиковать без координации (см. CommandHistoryRepository.upsert).
"""

from __future__ import annotations

from typing import Any

from ._base import BaseHistoryProducer


class CommandHistoryProducer(BaseHistoryProducer):
    """Канал `command_history` → POST /internal/api/v1/commands/history."""

    CHANNEL = "command_history"
    ENDPOINT = "/internal/api/v1/commands/history"

    async def publish_created(
        self,
        command_id: str,
        locker_id: str,
        command_type: str,
        *,
        parameters: dict[str, Any] | None = None,
        created_by: int | None = None,
        locker_inventory_number: str | None = None,
    ) -> int:
        return await self.enqueue(self._payload(
            command_id=command_id,
            locker_id=locker_id,
            command_type=command_type,
            status="pending",
            parameters=parameters,
            created_by=created_by,
            locker_inventory_number=locker_inventory_number,
        ))

    async def publish_sent(
        self,
        command_id: str,
        locker_id: str,
        command_type: str,
        *,
        parameters: dict[str, Any] | None = None,
        created_by: int | None = None,
        locker_inventory_number: str | None = None,
    ) -> int:
        return await self.enqueue(self._payload(
            command_id=command_id,
            locker_id=locker_id,
            command_type=command_type,
            status="sent",
            parameters=parameters,
            created_by=created_by,
            locker_inventory_number=locker_inventory_number,
        ))

    async def publish_executed(
        self,
        command_id: str,
        locker_id: str,
        command_type: str,
        *,
        result: dict[str, Any] | None = None,
        parameters: dict[str, Any] | None = None,
        created_by: int | None = None,
        locker_inventory_number: str | None = None,
    ) -> int:
        return await self.enqueue(self._payload(
            command_id=command_id,
            locker_id=locker_id,
            command_type=command_type,
            status="executed",
            result=result,
            parameters=parameters,
            created_by=created_by,
            locker_inventory_number=locker_inventory_number,
        ))

    async def publish_failed(
        self,
        command_id: str,
        locker_id: str,
        command_type: str,
        *,
        error_message: str,
        parameters: dict[str, Any] | None = None,
        created_by: int | None = None,
        locker_inventory_number: str | None = None,
    ) -> int:
        return await self.enqueue(self._payload(
            command_id=command_id,
            locker_id=locker_id,
            command_type=command_type,
            status="failed",
            error_message=error_message,
            parameters=parameters,
            created_by=created_by,
            locker_inventory_number=locker_inventory_number,
        ))

    @staticmethod
    def _payload(
        *,
        command_id: str,
        locker_id: str,
        command_type: str,
        status: str,
        parameters: dict[str, Any] | None = None,
        created_by: int | None = None,
        locker_inventory_number: str | None = None,
        result: dict[str, Any] | None = None,
        error_message: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "command_id": str(command_id),
            "locker_id": str(locker_id),
            "command_type": command_type,
            "status": status,
        }
        if parameters is not None:
            body["parameters"] = parameters
        if created_by is not None:
            body["created_by"] = created_by
        if locker_inventory_number is not None:
            body["locker_inventory_number"] = locker_inventory_number
        if result is not None:
            body["result"] = result
        if error_message is not None:
            body["error_message"] = error_message
        return body
