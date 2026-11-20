"""HTTP-клиент для интеграции с notification_api."""

from __future__ import annotations

from datetime import UTC, datetime
import json
import logging
import os
from typing import Any
import urllib.parse

from tornado.httpclient import HTTPError as TornadoHTTPError

from shared.http import RetryHTTPClient, UpstreamUnavailableError

logger = logging.getLogger(__name__)

NOTIFICATION_API_URL = os.getenv("NOTIFICATION_API_URL", "http://notification_api:8005")




class NotificationClient:
    """Async HTTP-клиент для создания и отмены уведомлений через notification_api."""

    def __init__(self, base_url: str = NOTIFICATION_API_URL):
        self._base = base_url.rstrip("/")
        self._http = RetryHTTPClient()

    async def create_persistent(
        self,
        text: str,
        tags: dict[str, Any],
        roles: list[str] | None = None,
        user_ids: list[int] | None = None,
    ) -> str | None:
        """Создать постоянное уведомление.

        Returns:
            ID созданного уведомления или None при ошибке.
        """
        body: dict[str, Any] = {
            "type": "persistent",
            "text": text,
            "tags": tags,
            "start_at": datetime.now(UTC).isoformat(),
        }
        if roles:
            body["roles"] = roles
        if user_ids:
            body["user_ids"] = user_ids

        try:
            resp = await self._http.fetch(
                f"{self._base}/internal/api/v1/notifications",
                method="POST",
                body=json.dumps(body),
                headers={"Content-Type": "application/json"},
            )
        except TornadoHTTPError as exc:
            if 400 <= exc.code < 500:
                # 4xx — наша ошибка контракта (валидация payload и т.п.),
                # ретрай не поможет; возвращаем None но логируем со stack
                # trace — без него не отличить «правильно сериализованный
                # body отвергнут» от «забыли передать поле в новой схеме».
                logger.warning(
                    "notification_api rejected create_persistent: %s",
                    exc, exc_info=True,
                )
                return None
            logger.error("notification_api 5xx on create_persistent: %s", exc)
            raise UpstreamUnavailableError(str(exc), status=exc.code, upstream='notification_api') from exc
        except (OSError, ConnectionError) as exc:
            logger.error("notification_api network failure on create_persistent: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream='notification_api') from exc

        payload = json.loads(resp.body)
        # WHY: POST /internal/api/v1/notifications возвращает
        # {"notification": {...}, "recipients": [...]}. Fallback на
        # `payload` сам сохраняет совместимость с тестовыми моками,
        # которые возвращают сразу плоский объект (см. test_notification_client).
        notification = payload.get("notification") or payload
        return notification.get("id") if isinstance(notification, dict) else None

    async def cancel(self, notification_id: str) -> bool:
        """Отменить уведомление по ID.

        Returns:
            True при успехе или 4xx (notification уже не существует — идемпотентно).

        Raises:
            UpstreamUnavailableError: 5xx или сетевой сбой — caller
                решает, эскалировать или swallow.
        """
        try:
            await self._http.fetch(
                f"{self._base}/internal/api/v1/notification/{notification_id}",
                method="DELETE",
                allow_nonstandard_methods=True,
            )
            return True
        except TornadoHTTPError as exc:
            if 400 <= exc.code < 500:
                logger.warning(
                    "notification_api 4xx on cancel %s: %s (idempotent treat as success)",
                    notification_id, exc,
                )
                return False
            logger.error("notification_api 5xx on cancel %s: %s", notification_id, exc)
            raise UpstreamUnavailableError(str(exc), upstream='notification_api') from exc
        except (OSError, ConnectionError) as exc:
            logger.error("notification_api network failure on cancel %s: %s", notification_id, exc)
            raise UpstreamUnavailableError(str(exc), upstream='notification_api') from exc

    async def find_active_by_tag(self, tag_key: str, tag_value: str) -> list[dict[str, Any]]:
        """Получить активные постоянные уведомления по тегу.

        Raises:
            UpstreamUnavailableError: 5xx или сетевой сбой.
        """
        params = urllib.parse.urlencode({
            "tag_key": tag_key,
            "tag_value": tag_value,
            "type": "persistent",
            "active_only": "true",
            "limit": "50",
        })
        try:
            resp = await self._http.fetch(
                f"{self._base}/internal/api/v1/notifications?{params}",
            )
        except TornadoHTTPError as exc:
            if 400 <= exc.code < 500:
                # 4xx тут означает сломанный фильтр или схема query — для
                # для триажа нужен stack trace — exc_info=True выбрасывается в структурный лог.
                logger.warning(
                    "notification_api 4xx on find_active_by_tag(%s=%s): %s",
                    tag_key, tag_value, exc, exc_info=True,
                )
                return []
            logger.error(
                "notification_api 5xx on find_active_by_tag(%s=%s): %s",
                tag_key, tag_value, exc,
            )
            raise UpstreamUnavailableError(str(exc), status=exc.code, upstream='notification_api') from exc
        except (OSError, ConnectionError) as exc:
            logger.error(
                "notification_api network failure on find_active_by_tag(%s=%s): %s",
                tag_key, tag_value, exc,
            )
            raise UpstreamUnavailableError(str(exc), upstream='notification_api') from exc

        payload = json.loads(resp.body)
        # GET возвращает {"items": [...], "total", "limit", "offset"}
        # после унификации пагинации.
        if isinstance(payload, dict):
            items = payload.get("items", [])
            if not isinstance(items, list):
                logger.error(
                    "notification_api find_active_by_tag returned non-list items: %s",
                    type(items).__name__,
                )
                raise UpstreamUnavailableError(
                    "notification_api returned non-list items",
                    upstream='notification_api',
                )
            return items
        if not isinstance(payload, list):
            logger.error(
                "notification_api find_active_by_tag returned unexpected payload: %s",
                type(payload).__name__,
            )
            raise UpstreamUnavailableError(
                "notification_api returned unexpected payload",
                upstream='notification_api',
            )
        return payload

    async def cancel_locker_open_notifications(self, locker_id: str) -> int:
        """Найти и отменить все активные уведомления об открытии шкафа.

        Returns:
            Количество отменённых уведомлений.
        """
        notifications = await self.find_active_by_tag("locker_id", locker_id)
        cancelled = 0
        for n in notifications:
            if n.get("tags", {}).get("event") == "locker_opened" and await self.cancel(n["id"]):
                cancelled += 1
        return cancelled
