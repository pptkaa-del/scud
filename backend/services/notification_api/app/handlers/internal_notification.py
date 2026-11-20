"""Internal handlers для создания/чтения/отмены уведомлений другими сервисами.

Эндпойнты `/internal/api/v1/notifications` и `/internal/api/v1/notification/{id}`
закрыты снаружи через Apache (<Location /internal> Require all denied).
Доступны только сервисам внутри Docker-сети — поэтому не требуют X-Remote-User
и не наследуются от BaseHandler, который этот заголовок проверяет.

Используется notification_client (locker_api) для системных уведомлений
(threshold violation, locker open и т.п.). created_by записывается как NULL —
событие порождено системой, а не конкретным пользователем.
"""

from __future__ import annotations

from .notification_logic import NotificationCancelLogic, NotificationListLogic


class InternalNotificationListHandler(NotificationListLogic):
    """GET/POST /internal/api/v1/notifications — системные уведомления."""

    async def get(self) -> None:
        await self._list_notifications()

    async def post(self) -> None:
        await self._create_notification(None)  # система-источник, не пользователь


class InternalNotificationDetailHandler(NotificationCancelLogic):
    """DELETE /internal/api/v1/notification/{id} — отмена уведомления системой."""

    async def delete(self, notification_id: str) -> None:
        await self._cancel_notification(notification_id)
