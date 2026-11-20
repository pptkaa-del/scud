import asyncio
from logging import getLogger
import os
import signal
from typing import Any

import psycopg2
import tornado.ioloop
import tornado.web

from shared.auth import (
    HealthHandler,
    LivenessHandler,
    ReadinessHandler,
    db_check,
    redis_check,
)
from shared.database import AsyncPostgres, PostgresConfig
from shared.metrics import register_pool
from shared.metrics.handler import MetricsHandler
from shared.observability import configure_logging
from shared.redis_client import RedisClient
from shared.utils import hash_password, is_production, read_secret

from .handlers.access import (
    UserAccessBatchHandler,
    UserAccessDetailHandler,
    UserAccessListHandler,
    UserAccessReactivateHandler,
)
from .handlers.contractor import ContractorDetailHandler, ContractorListHandler
from .handlers.emergency_request import (
    EmergencyRequestDetailHandler,
    EmergencyRequestListHandler,
    EmergencyRequestLogsHandler,
)
from .handlers.internal_auth import (
    InternalAuthCheckHandler,
    InternalUserByIdHandler,
    InternalUserByPhoneHandler,
    InternalUserNamesHandler,
)
from .handlers.internal_max_binding import InternalMaxBindingHandler
from .handlers.internal_max_context import InternalMaxContextHandler
from .handlers.user import (
    UserDisable2FAHandler,
    UserEnable2FAHandler,
    UserHandler,
    UserListHandler,
    UserReactivateHandler,
)
from .services.auth_session_client import AuthApiSessionClient
from .services.cache_invalidator import CacheInvalidator
from .user_data.db.models.user import UserRole
from .user_data.db.repositories.user import UserRepository

logger = getLogger(__name__)

def make_app(
    db: AsyncPostgres,
    cache_invalidator: CacheInvalidator | None = None,
    redis_client: RedisClient | None = None,
) -> tornado.web.Application:
    debug = os.getenv("DEBUG", "false").lower() == "true"
    settings: dict[str, Any] = {"debug": debug}
    if cache_invalidator is not None:
        settings["cache_invalidator"] = cache_invalidator
    # Общий с auth_api секрет для проверки подписи запросов
    # ``/internal/auth/check``. Если ENV пуст — handler пропускает проверку
    # (обратная совместимость для dev/CI). В production обязателен.
    # читаем сначала из /run/secrets/internal_auth_hmac_key, иначе env.
    internal_hmac_key = read_secret(
        "internal_auth_hmac_key",
        env_var="INTERNAL_AUTH_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    if internal_hmac_key:
        settings["internal_auth_hmac_key"] = internal_hmac_key.encode("utf-8")
    else:
        logger.warning(
            "[SECURITY] INTERNAL_AUTH_HMAC_KEY is not set — /internal/auth/check "
            "will accept UNSIGNED requests. Set the same value in auth_api and "
            "user_api before production deploy."
        )

    # Ключ для подписи запросов /internal/user/by-invite от notification_api'шного
    # MaxBotPoller. Разделён с internal_auth_hmac_key — компрометация одного
    # канала не открывает доступа к другому.
    max_binding_hmac_key = read_secret(
        "internal_max_binding_hmac_key",
        env_var="INTERNAL_MAX_BINDING_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    if max_binding_hmac_key:
        settings["internal_max_binding_hmac_key"] = max_binding_hmac_key.encode("utf-8")
    else:
        logger.warning(
            "[SECURITY] INTERNAL_MAX_BINDING_HMAC_KEY is not set — /internal/user/by-invite "
            "will accept UNSIGNED requests. Set the same value in user_api and "
            "notification_api before production deploy."
        )

    # Ключ для подписи PII-read запросов /internal/user/by-phone,
    # /internal/user/{id}, /internal/users/names от auth_api и reports_api.
    # Разделён с прочими каналами — компрометация одного ключа не открывает
    # доступа к остальным inter-service path (SEC-10).
    user_read_hmac_key = read_secret(
        "internal_user_read_hmac_key",
        env_var="INTERNAL_USER_READ_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    if user_read_hmac_key:
        settings["internal_user_read_hmac_key"] = user_read_hmac_key.encode("utf-8")
    else:
        logger.warning(
            "[SECURITY] INTERNAL_USER_READ_HMAC_KEY is not set — "
            "/internal/user/{by-phone,by-id,names} will accept UNSIGNED "
            "requests. Set the same value in user_api, auth_api and "
            "reports_api before production deploy."
        )

    # Ключ для подписи запросов /internal/user/{id}/max-context от auth_api
    # step-up flow. Разделён с internal_auth_hmac_key — компрометация одного
    # канала не открывает доступа к другому.
    step_up_hmac_key_str = read_secret(
        "internal_step_up_hmac_key",
        env_var="INTERNAL_STEP_UP_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    if step_up_hmac_key_str:
        settings["internal_step_up_hmac_key"] = step_up_hmac_key_str.encode("utf-8")
    else:
        logger.warning(
            "[SECURITY] INTERNAL_STEP_UP_HMAC_KEY is not set — "
            "/internal/user/{id}/max-context will accept UNSIGNED requests. "
            "Set the same value in user_api and auth_api before production deploy."
        )

    # Ключ для подписи исходящих /internal/api/v1/sessions/revoke в auth_api:
    # после смены пароля / 2FA user_api отзывает все живые сессии пользователя
    # (SEC-14). Разделён per-domain. Если ключ не задан — запрос уйдёт без
    # подписи (dev/CI), auth_api в таком же режиме его пропустит.
    session_revoke_hmac_key = read_secret(
        "internal_session_revoke_hmac_key",
        env_var="INTERNAL_SESSION_REVOKE_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    session_revoke_key_bytes: bytes | None = (
        session_revoke_hmac_key.encode("utf-8") if session_revoke_hmac_key else None
    )
    if session_revoke_key_bytes is None:
        logger.warning(
            "[SECURITY] INTERNAL_SESSION_REVOKE_HMAC_KEY is not set — "
            "/internal/api/v1/sessions/revoke requests will be sent UNSIGNED. "
            "Set the same value in user_api and auth_api before production deploy."
        )

    # Клиент отзыва сессий: user_api → auth_api после смены учётных данных.
    auth_api_url = os.getenv("AUTH_API_URL", "http://auth_api:8003")
    settings["auth_session_client"] = AuthApiSessionClient(
        auth_api_url, hmac_key=session_revoke_key_bytes,
    )

    # Имя Max-бота для генерации deep-link при создании пользователя.
    # Если пусто — endpoint вернёт max_invite_link=null, ссылку соберёт фронт.
    settings["max_bot_username"] = os.getenv("MAX_BOT_USERNAME", "")

    return tornado.web.Application([
        # Health-depth: /live (рестарт-проба оркестратора, без БД),
        # /ready (трафик/алерты — db critical + redis soft), /health
        # (DB-only, обратная совместимость с docker healthcheck).
        (r"/live", LivenessHandler),
        (r"/ready", ReadinessHandler, dict(
            checks=[db_check(db)] + (
                [redis_check(redis_client)] if redis_client is not None else []
            ),
        )),
        (r"/health", HealthHandler, dict(db=db)),
        # Prometheus scrape endpoint. Без auth, доступен только из
        # backend_net (Apache gateway не проксирует /metrics наружу).
        (r"/metrics", MetricsHandler),
        # User access routes (должны быть до /users/{user_id} для правильного матчинга)
        (r"/api/v1/user/([^/]+)/accesses", UserAccessListHandler, dict(db=db)),
        (r"/api/v1/accesses/batch", UserAccessBatchHandler, dict(db=db)),
        (r"/api/v1/access/([^/]+)/reactivate", UserAccessReactivateHandler, dict(db=db)),
        (r"/api/v1/access/([^/]+)", UserAccessDetailHandler, dict(db=db)),
        # User routes
        (r"/api/v1/user/([^/]+)/reactivate", UserReactivateHandler, dict(db=db)),
        (r"/api/v1/user/([^/]+)/enable-2fa", UserEnable2FAHandler, dict(db=db)),
        (r"/api/v1/user/([^/]+)/disable-2fa", UserDisable2FAHandler, dict(db=db)),
        (r"/api/v1/user/([^/]+)", UserHandler, dict(db=db)),
        (r"/api/v1/users", UserListHandler, dict(db=db)),
        # Contractor routes
        (r"/api/v1/contractor/([^/]+)", ContractorDetailHandler, dict(db=db)),
        (r"/api/v1/contractors", ContractorListHandler, dict(db=db)),
        # Emergency request routes
        (r"/api/v1/emergency-request/([^/]+)/logs", EmergencyRequestLogsHandler, dict(db=db)),
        (r"/api/v1/emergency-request/([^/]+)", EmergencyRequestDetailHandler, dict(db=db)),
        (r"/api/v1/emergency-requests", EmergencyRequestListHandler, dict(db=db)),
        # Internal endpoints (доступны только внутри backend_net, закрыты Apache снаружи)
        (r"/internal/auth/check", InternalAuthCheckHandler, dict(db=db)),
        (r"/internal/user/by-phone", InternalUserByPhoneHandler, dict(db=db)),
        (r"/internal/user/by-invite", InternalMaxBindingHandler, dict(db=db)),
        (r"/internal/users/names", InternalUserNamesHandler, dict(db=db)),
        (r"/internal/user/(\d+)/max-context", InternalMaxContextHandler, dict(db=db)),
        (r"/internal/user/(\d+)", InternalUserByIdHandler, dict(db=db)),
    ], **settings)

async def wait_for_db(config: PostgresConfig) -> AsyncPostgres:
    # Ожидание готовности БД с ретраями и экспоненциальной задержкой
    attempts, delay, max_attempts = 0, 1.0, 30
    while True:
        try:
            return AsyncPostgres(config, statement_timeout_ms=5000)
        except psycopg2.OperationalError as e:
            attempts += 1
            if attempts >= max_attempts:
                logger.error(f"Failed to connect to DB after {max_attempts} attempts.")
                raise
            logger.warning(f"DB not ready ({e}); retrying in {delay:.1f}s [{attempts}/{max_attempts}]...")
            await asyncio.sleep(delay)
            # Экспоненциальная задержка до 10 секунд
            delay = min(delay * 1.5, 10.0)

async def main() -> None:
    configure_logging(os.getenv("SERVICE_NAME", "user_api"))
    # Настройки БД из окружения или дефолтные
    # пароль БД читается из Docker secret или env-fallback.
    db_password = read_secret(
        "postgres_user_password", env_var="DB_PASSWORD", default="postgres"
    )
    config = PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "postgres"),
        password=db_password,
        database=os.getenv("DB_NAME", "scud"),
    )

    # Ожидание готовности БД
    db = await wait_for_db(config)

    # Pull-based DB-pool-saturation метрики: семплируются на scrape /metrics.
    register_pool(db.pool_name, db.get_pool_info)

    # Redis клиент для cross-service инвалидации ACM cache.
    # REDIS_URL берётся из env (для случая когда redis вынесен наружу),
    # иначе собираем URL из read_secret(redis_password) + дефолтного хоста.
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        redis_password = read_secret(
            "redis_password",
            env_var="REDIS_PASSWORD",
            default="redis-dev-only-do-not-use-in-prod",
        )
        redis_url = f"redis://:{redis_password}@redis:6379/0"
    redis_client = RedisClient.from_url(redis_url)
    cache_invalidator = CacheInvalidator(redis_client)

    # Создание администратора при первом старте (idempotent)
    admin_phone = os.getenv("ADMIN_PHONE", "")
    # admin_password из Docker secret или env-fallback.
    admin_password = read_secret(
        "admin_password", env_var="ADMIN_PASSWORD", default=""
    ) or ""
    if admin_phone and admin_password:
        repo = UserRepository(db)
        existing = await repo.get_by_phone(admin_phone)
        if not existing:
            admin_name = os.getenv("ADMIN_FULL_NAME", "Администратор")
            user = await repo.create(phone=admin_phone, full_name=admin_name,
                                     created_by=None, role=UserRole.ADMINISTRATOR)
            # Bootstrap-админ — единственное исключение из 2FA-по-умолчанию:
            # на старте системы у него ещё нет привязки Max, а войти он должен.
            await repo.set_password_hash(user.id, hash_password(admin_password), require_2fa=False)
            logger.info("Admin user created: id=%s", user.id)
        else:
            logger.info("Admin user already exists: id=%s", existing.id)

    app = make_app(db, cache_invalidator=cache_invalidator, redis_client=redis_client)
    port = int(os.getenv("APP_PORT", 8000))
    app.listen(port, xheaders=True)
    logger.info(f"User API started on port {port}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    logger.info("User API started, waiting for shutdown signal")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        try:
            await redis_client.close()
        except Exception:
            logger.warning("Error closing redis client", exc_info=True)
        db.close()

if __name__ == "__main__":
    asyncio.run(main())
