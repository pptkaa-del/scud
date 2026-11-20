from __future__ import annotations

import logging

from shared.auth import ProblemDetailsHandler

from ..notification_data.db.models.notification import NotificationType
from ..notification_data.db.repositories.notification import NotificationRepository
from .dto import CreateNotificationDTO, PaginatedResponse

logger = logging.getLogger(__name__)


class NotificationListLogic(ProblemDetailsHandler):
    """Shared list/create logic for public + internal notification list handlers.

    Subclasses differ only in auth (public mixes in UserAuthMixin) and in the
    created_by attribution passed to _create_notification.
    """

    async def _list_notifications(self) -> None:
        type_filter = self.get_query_argument("type", default=None)
        user_id = self.get_query_argument("user_id", default=None)
        role = self.get_query_argument("role", default=None)
        tag_key = self.get_query_argument("tag_key", default=None)
        tag_value = self.get_query_argument("tag_value", default=None)
        active_only = (self.get_query_argument("active_only", default="true") or "true").lower() == "true"

        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        user_id_int = int(user_id) if user_id else None

        repo = NotificationRepository(self.db)
        notifications = await repo.get_all(
            type=type_filter,
            user_id=user_id_int,
            role=role,
            tag_key=tag_key,
            tag_value=tag_value,
            active_only=active_only,
            limit=limit,
            offset=offset,
        )
        total = await repo.count(
            type=type_filter,
            user_id=user_id_int,
            role=role,
            tag_key=tag_key,
            tag_value=tag_value,
            active_only=active_only,
        )

        self.write_json(
            PaginatedResponse(items=notifications, total=total, limit=limit, offset=offset)
        )

    async def _create_notification(self, created_by: int | None) -> None:
        dto = self.get_json_body_as(CreateNotificationDTO)

        notification_type = NotificationType(dto.type)

        repo = NotificationRepository(self.db)
        notification = await repo.create(
            type=notification_type,
            text=dto.text,
            tags=dto.tags,
            created_by=created_by,
            start_at=dto.start_at,
            user_ids=dto.user_ids,
            roles=dto.roles,
        )

        recipients = await repo.get_recipients(notification.id)

        self.write_json({
            "notification": notification,
            "recipients": recipients,
        }, status_code=201)


class NotificationCancelLogic(ProblemDetailsHandler):
    """Shared cancel logic for public + internal notification detail handlers."""

    async def _cancel_notification(self, notification_id: str) -> None:
        repo = NotificationRepository(self.db)
        notification = await repo.cancel(notification_id)

        if not notification:
            self.write_not_found("Notification not found or already cancelled")
            return

        self.write_json(notification)
