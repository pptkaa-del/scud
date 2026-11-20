"""
CacheInvalidator — fire-and-forget Redis DEL для user_ctx:{id}.

Вызывается при deactivate/reactivate/role-change в user_api для мгновенной
инвалидации ACM cache: иначе изменение видно только после истечения TTL.

Не блокирует основной flow при Redis-down: ошибка логируется, но не
проброшена — следующий ACM-запрос всё равно увидит свежее состояние через TTL.
"""
from __future__ import annotations

import logging

from shared.redis_client import RedisClient

logger = logging.getLogger(__name__)


class CacheInvalidator:
    """Тонкий wrapper над RedisClient для DEL user_ctx:{user_id}."""

    KEY_PREFIX = "user_ctx:"

    def __init__(self, redis_client: RedisClient) -> None:
        self._redis = redis_client

    async def invalidate_user(self, user_id: int) -> None:
        """Удаляет cached UserContext в ACM. Не пробрасывает Redis-ошибки."""
        try:
            await self._redis.delete(f"{self.KEY_PREFIX}{user_id}")
        except Exception:
            logger.warning(
                "Failed to invalidate user_ctx:%d (Redis error)",
                user_id, exc_info=True,
            )
