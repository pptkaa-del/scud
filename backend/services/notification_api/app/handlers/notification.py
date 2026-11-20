import logging

from shared.auth import UserAuthMixin

from ..notification_data.db.repositories.notification import NotificationRepository
from .notification_logic import NotificationCancelLogic, NotificationListLogic

logger = logging.getLogger(__name__)


class NotificationListHandler(UserAuthMixin, NotificationListLogic):
    """Handler для работы со списком уведомлений"""

    async def get(self) -> None:
        """Получить список уведомлений с фильтрацией и пагинацией."""
        await self._list_notifications()

    async def post(self) -> None:
        """Создать новое уведомление"""
        await self._create_notification(self.current_user_id)


class NotificationDetailHandler(UserAuthMixin, NotificationCancelLogic):
    """Handler для работы с конкретным уведомлением"""

    async def get(self, notification_id: str) -> None:
        """Получить уведомление по ID с получателями"""
        repo = NotificationRepository(self.db)
        notification = await repo.get_by_id(notification_id)

        if not notification:
            self.write_not_found("Notification not found")
            return

        recipients = await repo.get_recipients(notification_id)

        self.write_json({
            "notification": notification,
            "recipients": recipients,
        })

    async def delete(self, notification_id: str) -> None:
        """Отменить уведомление"""
        await self._cancel_notification(notification_id)
