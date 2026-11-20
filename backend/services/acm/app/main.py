"""
ACM — Модуль контроля разделения доступа.

Tornado-сервис (порт 8004), вызываемый Apache Lua перед проксированием каждого запроса.
Валидирует токен, проверяет права по матрице разрешений, возвращает контекстные заголовки.
"""
import asyncio
import contextlib
import logging
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
from shared.metrics import register_pool, set_redis_up
from shared.metrics.handler import MetricsHandler
from shared.observability import configure_logging
from shared.redis_client import RedisClient
from shared.utils import read_secret

from .cache import UserContextCache
from .handlers.check import CheckHandler
from .handlers.identity import IdentityHandler

logger = logging.getLogger(__name__)


async def _redis_up_sampler(redis_client: RedisClient, interval: float = 15.0) -> None:
    """Периодически семплирует Redis-доступность и выставляет scud_redis_up.

    Никогда не даёт исключению выйти за пределы цикла (кроме CancelledError при
    shutdown). Ошибки ping→ set_redis_up(False) и продолжает цикл.
    """
    while True:
        try:
            up = await redis_client.ping()
            set_redis_up(up)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Redis ping failed in sampler", exc_info=True)
            set_redis_up(False)
        await asyncio.sleep(interval)


def make_app(db: AsyncPostgres, settings: dict[str, Any]) -> tornado.web.Application:
    handler_kwargs = dict(db=db)
    redis_client = settings["redis_client"]
    return tornado.web.Application(
        [
            # Internal: вызывается Apache Lua для проверки прав
            (r"/acm/check", CheckHandler, handler_kwargs),
            (r"/acm/identity", IdentityHandler, handler_kwargs),
            # Health-depth: /live (рестарт-проба оркестратора, без БД),
            # /ready (трафик/алерты — db critical + redis soft), /health
            # (DB-only, обратная совместимость с docker healthcheck).
            (r"/live", LivenessHandler),
            (r"/ready", ReadinessHandler, dict(
                checks=[db_check(db), redis_check(redis_client)],
            )),
            (r"/health", HealthHandler, handler_kwargs),
            # Prometheus scrape endpoint. См. user_api/main.py.
            (r"/metrics", MetricsHandler),
        ],
        **settings,
        debug=False,
    )


async def wait_for_db(config: PostgresConfig) -> AsyncPostgres:
    """Ожидание готовности БД с ретраями и экспоненциальной задержкой."""
    attempts, delay, max_attempts = 0, 1.0, 30
    while True:
        try:
            return AsyncPostgres(config, statement_timeout_ms=5000)
        except psycopg2.OperationalError as e:
            attempts += 1
            if attempts >= max_attempts:
                logger.error("Failed to connect to DB after %d attempts.", max_attempts)
                raise
            logger.warning("DB not ready (%s); retrying in %.1fs [%d/%d]...", e, delay, attempts, max_attempts)
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 10.0)


def build_db_config() -> PostgresConfig:
    """Собрать конфиг подключения ACM к scud_users из env/секретов.

    ACM — read-only клиент scud_users; ходит под least-privilege ролью acm_ro
    (SELECT-only, SEC-24), а не под admin. Пароль — из отдельного секрета
    acm_db_password (read_secret: /run/secrets → env DB_PASSWORD → dev-default).
    """
    db_password = read_secret(
        "acm_db_password", env_var="DB_PASSWORD", default="acm_ro_dev_password"
    )
    return PostgresConfig(
        host=os.getenv("DB_HOST", "postgres_user"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "acm_ro"),
        password=db_password,
        database=os.getenv("DB_NAME", "scud_users"),
        application_name="acm",
    )


async def main() -> None:
    configure_logging(os.getenv("SERVICE_NAME", "acm"))

    config = build_db_config()

    db = await wait_for_db(config)

    # Pull-based DB-pool-saturation метрики: семплируются на scrape /metrics.
    register_pool(db.pool_name, db.get_pool_info)

    # REDIS_URL приоритетно из env (если оператор переопределил полный URL,
    # например для внешнего Redis), иначе собираем из read_secret(redis_password).
    redis_url = os.getenv("REDIS_URL")
    if not redis_url:
        redis_password = read_secret(
            "redis_password",
            env_var="REDIS_PASSWORD",
            default="redis-dev-only-do-not-use-in-prod",
        )
        redis_url = f"redis://:{redis_password}@redis:6379/0"
    redis_client = RedisClient.from_url(redis_url)
    redis_up_task = asyncio.create_task(_redis_up_sampler(redis_client))
    cache_ttl = int(os.getenv("CACHE_TTL_SECONDS", "30"))
    cache = UserContextCache(redis_client, ttl_seconds=cache_ttl)

    app_settings = {
        "auth_api_url": os.getenv("AUTH_API_URL", "http://auth_api:8003"),
        "cache_ttl_seconds": cache_ttl,
        "redis_client": redis_client,
        "cache": cache,
    }

    app = make_app(db, app_settings)
    port = int(os.getenv("APP_PORT", 8004))
    app.listen(port, xheaders=True)
    logger.info("ACM started on port %d", port)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    logger.info("ACM started, waiting for shutdown signal")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        redis_up_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await redis_up_task
        try:
            await redis_client.close()
        except Exception:
            logger.warning("Error closing redis client", exc_info=True)
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
