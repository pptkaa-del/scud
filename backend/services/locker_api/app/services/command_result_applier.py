"""Общий аппликатор побочных эффектов результата команды (SIMP-10).

Физический локер рапортует о выполнении OPEN/CLOSE-команды двумя путями: WS
(``device_handler._handle_command_result``) и MQTT-relay
(``mqtt.results.RelayCommandResults.handle_rep``). Оба обязаны на УСПЕШНЫЙ
результат синхронно обновить ``lockers.door`` и создать/снять постоянное
уведомление об открытии. Раньше эта логика жила только в WS-пути, а MQTT-путь
её ронял — дверь уходила в рассинхрон, уведомление не создавалось/не снималось.

Чтобы пути не разъезжались снова, door-update + notification вынесены сюда в
единую точку. Аппликатор не знает ничего про WS-handler-специфичное состояние
(кэш двери и т.п.) — это остаётся в вызывающем коде.

Уведомления — best-effort: сбой notification-колбэка логируется и проглатывается,
обработку результата команды он не валит (паритет с WS-helper'ами, которые сами
ловят UpstreamUnavailableError).
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging
from typing import Any, Protocol

from ..locker_data.db.models.command import CommandType
from ..locker_data.db.models.locker import DoorState

logger = logging.getLogger(__name__)

# Колбэки уведомлений: open принимает команду, close — locker_id. Оба best-effort.
NotifyOpened = Callable[[Any], Awaitable[None]]
NotifyClosed = Callable[[str], Awaitable[None]]


class _DoorUpdater(Protocol):
    async def update_door(
        self, locker_id: str, door: DoorState, who_open_last: int | None = None,
    ) -> Any: ...


class CommandResultApplier:
    """Применяет door-state + notification побочные эффекты результата команды.

    Параметризуется ``LockerRepository`` (порт ``update_door``) и двумя
    notification-колбэками. Это позволяет WS-пути переиспользовать свои
    ``_notify_locker_opened`` / ``_notify_locker_closed`` (которые тестам удобно
    стабить), а MQTT-пути — построить колбэки поверх ``NotificationClient``.
    """

    def __init__(
        self,
        locker_repo: _DoorUpdater,
        notify_opened: NotifyOpened,
        notify_closed: NotifyClosed,
    ) -> None:
        self._locker_repo = locker_repo
        self._notify_opened = notify_opened
        self._notify_closed = notify_closed

    async def apply_success(self, command: Any) -> None:
        """Побочные эффекты успешного результата OPEN/CLOSE-команды.

        OPEN  → update_door(OPEN, who_open_last=created_by) + notify_opened.
        CLOSE → update_door(CLOSED) + notify_closed.
        Прочие типы (REBOOT/CONFIG) дверь/уведомления не трогают.
        """
        if command.type == CommandType.OPEN:
            await self._notify_opened(command)
            await self._locker_repo.update_door(
                command.locker_id, DoorState.OPEN, who_open_last=command.created_by,
            )
        elif command.type == CommandType.CLOSE:
            await self._notify_closed(command.locker_id)
            await self._locker_repo.update_door(command.locker_id, DoorState.CLOSED)
