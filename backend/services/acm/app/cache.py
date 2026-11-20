"""
Redis-backed TTL-кэш контекста пользователя.

Кэш в Redis позволяет user_api инвалидировать запись (DEL user_ctx:{id})
при изменении state/role, чтобы закрыть TTL-окно между деактивацией/сменой
роли и реальным истечением ключа. Истечение TTL обрабатывается самим Redis,
поэтому evict_expired/size в API не нужны.
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import json
import logging

from shared.redis_client import RedisClient, RedisError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UserContext:
    """Данные пользователя, полученные из БД и закэшированные для проверки доступа."""
    user_id: int
    role: str           # "worker" | "operator" | "administrator"
    state: str          # "active" | "deactivated" | "deleted"
    full_access: bool   # True — доступ ко всем шкафам
    locker_ids: tuple[str, ...]  # UUID шкафов с активным Допуском
    require_2fa: bool = False    # требуется 2FA для critical-операций (OPEN/CLOSE)


class UserContextCache:
    """Redis-backed cache. Async API.

    Ключи хранятся как user_ctx:{user_id} в общем Redis инстансе,
    к которому также подключён user_api для cross-service инвалидации.
    """

    KEY_PREFIX = "user_ctx:"

    def __init__(self, redis_client: RedisClient, ttl_seconds: int = 30) -> None:
        self._redis = redis_client
        self._ttl = ttl_seconds
        # Singleflight: при cache miss несколько одновременных запросов одного
        # user_id ждут одну future вместо параллельной загрузки из БД.
        # Без этого при рестарте Redis/ACM 500 одновременных запросов от 100
        # пользователей дали бы 100×2 = 200 параллельных SQL — пул задавился бы.
        self._pending: dict[int, asyncio.Future[UserContext | None]] = {}

    def _key(self, user_id: int) -> str:
        return f"{self.KEY_PREFIX}{user_id}"

    async def get(self, user_id: int) -> UserContext | None:
        """Возвращает закэшированный контекст или None если истёк / отсутствует / повреждён.

        Сбой Redis (RedisError, в т.ч. RedisConnectionError при таймауте/обрыве)
        трактуется как cache-miss → None: ACM деградирует к загрузке из БД, а не
        отвечает 500 на каждую auth-проверку при кратковременной недоступности
        Redis. ACM гейтит КАЖДЫЙ аутентифицированный запрос, поэтому отказ кэша
        не должен быть фатальным.
        """
        try:
            raw = await self._redis.get(self._key(user_id))
        except RedisError:
            logger.warning(
                "Redis GET failed for user %d, treating as cache miss", user_id,
            )
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            logger.warning("Corrupt cache entry for user %d, treating as miss", user_id)
            return None
        try:
            return UserContext(
                user_id=user_id,
                role=data["role"],
                state=data["state"],
                full_access=data["full_access"],
                locker_ids=tuple(data["locker_ids"]),
                require_2fa=bool(data.get("require_2fa", False)),
            )
        except (KeyError, TypeError):
            logger.warning("Malformed cache entry for user %d, treating as miss", user_id)
            return None

    async def set(self, ctx: UserContext) -> None:
        """Сохраняет контекст пользователя с TTL.

        Сбой записи в Redis (RedisError) не фатален: значение уже загружено из
        БД и будет возвращено вызывающему; неудача кэширования лишь означает,
        что следующий запрос снова сходит в БД. Логируем как warning и
        проглатываем, чтобы не уронить auth-проверку.
        """
        payload = json.dumps({
            "role": ctx.role,
            "state": ctx.state,
            "full_access": ctx.full_access,
            "locker_ids": list(ctx.locker_ids),
            "require_2fa": ctx.require_2fa,
        })
        try:
            await self._redis.set(self._key(ctx.user_id), payload, ex=self._ttl)
        except RedisError:
            logger.warning(
                "Redis SET failed for user %d, value not cached (non-fatal)",
                ctx.user_id,
            )

    async def invalidate(self, user_id: int) -> None:
        """Принудительно удаляет запись (например, после деактивации).

        Сбой Redis не фатален: запись всё равно истечёт по TTL, а вызывающий
        (CheckHandler при неактивном пользователе) уже принял решение по
        свежезагруженному ctx.state — отказ инвалидации не должен превращать
        корректный 403 в 500.
        """
        try:
            await self._redis.delete(self._key(user_id))
        except RedisError:
            logger.warning(
                "Redis DEL failed for user %d, relying on TTL expiry", user_id,
            )

    async def get_or_load(
        self,
        user_id: int,
        loader: Callable[[int], Awaitable[UserContext | None]],
    ) -> UserContext | None:
        """Singleflight-вариант ``get``: при miss один загрузчик, остальные ждут.

        ``loader`` — async-функция ``(user_id) -> Optional[UserContext]``,
        обычно ``CheckHandler._load_user_context``. На успех загрузки кэш
        обновляется. На ``loader → None`` (пользователь не найден) кэш не
        обновляется — отрицательный результат не закрепляется.
        """
        ctx = await self.get(user_id)
        if ctx is not None:
            return ctx

        pending = self._pending.get(user_id)
        if pending is not None:
            return await pending

        loop = asyncio.get_event_loop()
        future: asyncio.Future[UserContext | None] = loop.create_future()
        self._pending[user_id] = future
        try:
            ctx = await loader(user_id)
            if ctx is not None:
                await self.set(ctx)
            future.set_result(ctx)
            return ctx
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            self._pending.pop(user_id, None)
