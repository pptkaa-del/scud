"""TTL команд шкафа: активная команда старше TTL переводится в терминальный failed."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from shared.enums import CommandTransition

from ..observability import command_metrics
from .history_producers.command_history_producer import CommandHistoryProducer

if TYPE_CHECKING:
    from ..locker_data.db.models.command import LockerCommand
    from ..locker_data.db.repositories.command import CommandRepository

logger = logging.getLogger(__name__)

DEFAULT_COMMAND_TTL_S = 60
COMMAND_TTL_FAILURE_REASON = "истёк срок ожидания ответа шкафа"
_SWEEP_BATCH = 500


class CommandExpiry:
    def __init__(self, ttl_s: int) -> None:
        if ttl_s <= 0:
            raise ValueError(f"COMMAND_TTL_S must be positive, got {ttl_s}")
        self._ttl_s = ttl_s

    @property
    def ttl_s(self) -> int:
        return self._ttl_s

    async def sweep(self, repo: CommandRepository) -> list[LockerCommand]:
        expired: list[LockerCommand] = await repo.expire_stale_commands(
            ttl_s=self._ttl_s,
            error_message=COMMAND_TTL_FAILURE_REASON,
            outbox_channel=CommandHistoryProducer.CHANNEL,
            outbox_endpoint=CommandHistoryProducer.ENDPOINT,
            limit=_SWEEP_BATCH,
        )
        for cmd in expired:
            logger.info(
                "Command %s (%s) on locker %s expired: no device result within %ss",
                cmd.id, cmd.type, cmd.locker_id, self._ttl_s,
            )
            command_metrics.record_transition(cmd.type, CommandTransition.TTL_EXPIRED)
        return expired

    async def drain(self, repo: CommandRepository) -> int:
        """Выметать батчами до короткого батча; используется на старте сервиса."""
        total = 0
        while True:
            batch = await self.sweep(repo)
            total += len(batch)
            if len(batch) < _SWEEP_BATCH:
                return total
