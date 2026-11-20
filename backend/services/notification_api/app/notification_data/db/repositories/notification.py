from __future__ import annotations

import json
import logging
from typing import Any

from dacite import Config, from_dict

from shared.database import AsyncPostgres, SqlParam

from ..models.notification import Notification, NotificationRecipient, NotificationType

logger = logging.getLogger(__name__)

_DACITE_CONFIG = Config(cast=[NotificationType])


class NotificationRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create(
        self,
        type: NotificationType,
        text: str,
        tags: dict[str, Any],
        created_by: int | None,
        start_at: str | None = None,
        user_ids: list[int] | None = None,
        roles: list[str] | None = None,
    ) -> Notification:
        row = await self._db.fetch_one_strict(
            """
            INSERT INTO notifications (
                type, text, tags, start_at, created_by
            )
            VALUES (%s, %s, %s::jsonb, %s, %s)
            RETURNING *
            """,
            (
                type.value,
                text,
                json.dumps(tags),
                start_at,
                created_by,
            ),
        )
        notification = from_dict(Notification, row, config=_DACITE_CONFIG)

        # Bulk-insert recipients
        if user_ids:
            for uid in user_ids:
                await self._db.execute(
                    """
                    INSERT INTO notification_recipients (notification_id, user_id)
                    VALUES (%s, %s)
                    """,
                    (notification.id, uid),
                )

        if roles:
            for role in roles:
                await self._db.execute(
                    """
                    INSERT INTO notification_recipients (notification_id, role)
                    VALUES (%s, %s)
                    """,
                    (notification.id, role),
                )

        return notification

    async def get_by_id(self, notification_id: str) -> Notification | None:
        row = await self._db.fetch_one(
            "SELECT * FROM notifications WHERE id = %s",
            (notification_id,),
        )
        if not row:
            return None
        return from_dict(Notification, row, config=_DACITE_CONFIG)

    async def get_recipients(self, notification_id: str) -> list[NotificationRecipient]:
        rows = await self._db.fetch_all(
            "SELECT * FROM notification_recipients WHERE notification_id = %s",
            (notification_id,),
        )
        return [from_dict(NotificationRecipient, row) for row in rows]

    async def get_all(
        self,
        type: str | None = None,
        user_id: int | None = None,
        role: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        active_only: bool = True,
        limit: int = 100,
        offset: int = 0,
    ) -> list[Notification]:
        query = "SELECT n.* FROM notifications n WHERE 1=1"
        params: list[SqlParam] = []

        if active_only:
            query += " AND n.is_cancelled = false"

        if type:
            query += " AND n.type = %s"
            params.append(type)

        if tag_key and tag_value:
            query += " AND n.tags @> %s::jsonb"
            params.append(json.dumps({tag_key: tag_value}))

        if user_id is not None:
            query += """
                AND EXISTS (
                    SELECT 1 FROM notification_recipients r
                    WHERE r.notification_id = n.id AND r.user_id = %s
                )
            """
            params.append(user_id)

        if role:
            query += """
                AND EXISTS (
                    SELECT 1 FROM notification_recipients r
                    WHERE r.notification_id = n.id AND r.role = %s
                )
            """
            params.append(role)

        query += " ORDER BY n.created_at DESC LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(Notification, row, config=_DACITE_CONFIG) for row in rows]

    async def count(
        self,
        type: str | None = None,
        user_id: int | None = None,
        role: str | None = None,
        tag_key: str | None = None,
        tag_value: str | None = None,
        active_only: bool = True,
    ) -> int:
        """Общее количество уведомлений с теми же фильтрами что и `get_all`."""
        query = "SELECT COUNT(*) AS count FROM notifications n WHERE 1=1"
        params: list[SqlParam] = []

        if active_only:
            query += " AND n.is_cancelled = false"

        if type:
            query += " AND n.type = %s"
            params.append(type)

        if tag_key and tag_value:
            query += " AND n.tags @> %s::jsonb"
            params.append(json.dumps({tag_key: tag_value}))

        if user_id is not None:
            query += """
                AND EXISTS (
                    SELECT 1 FROM notification_recipients r
                    WHERE r.notification_id = n.id AND r.user_id = %s
                )
            """
            params.append(user_id)

        if role:
            query += """
                AND EXISTS (
                    SELECT 1 FROM notification_recipients r
                    WHERE r.notification_id = n.id AND r.role = %s
                )
            """
            params.append(role)

        row = await self._db.fetch_one(query, tuple(params))
        return int(row["count"]) if row else 0

    async def cancel(self, notification_id: str) -> Notification | None:
        row = await self._db.fetch_one(
            """
            UPDATE notifications
            SET is_cancelled = true
            WHERE id = %s AND is_cancelled = false
            RETURNING *
            """,
            (notification_id,),
            read_only=False,
        )
        if not row:
            return None
        return from_dict(Notification, row, config=_DACITE_CONFIG)
