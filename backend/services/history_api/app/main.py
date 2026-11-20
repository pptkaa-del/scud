"""
History API - Точка входа приложения.

Микросервис для хранения и аналитики исторических данных СКУД.
"""

import asyncio
import logging
import os
import signal

import psycopg2
import tornado.ioloop
import tornado.web

from shared.auth import (
    HealthHandler,
    LivenessHandler,
    ReadinessHandler,
    db_check,
)
from shared.database import AsyncPostgres, PostgresConfig
from shared.metrics import register_pool
from shared.metrics.handler import MetricsHandler
from shared.observability import configure_logging
from shared.utils import is_production, read_secret

from .handlers.analytics import LockerSummaryHandler, SensorAnalyticsHandler
from .handlers.command_history import CommandHistoryHandler
from .handlers.internal_command_history import InternalCommandHistoryHandler
from .handlers.internal_security_event import InternalSecurityEventHandler
from .handlers.internal_telemetry import InternalTelemetryHandler
from .handlers.media import (
    InternalMediaUploadHandler,
    MediaDetailHandler,
    MediaListByEventHandler,
    MediaListHandler,
)
from .handlers.security_events import SecurityEventDetailHandler, SecurityEventListHandler
from .handlers.telemetry import TelemetryBatchHandler, TelemetryListHandler
from .services.aggregation import AggregationService
from .services.retention import RetentionService

logger = logging.getLogger(__name__)


async def wait_for_db(config: PostgresConfig) -> AsyncPostgres:
    """Ожидание готовности БД с ретраями и экспоненциальной задержкой."""
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
            delay = min(delay * 1.5, 10.0)


def make_app(
    db: AsyncPostgres,
    *,
    internal_history_hmac_key: str | None = None,
) -> tornado.web.Application:
    """
    Создание Tornado приложения с маршрутами.

    Args:
        db: Подключение к БД
        internal_history_hmac_key: общий HMAC-ключ для проверки подписи на
            internal write-эндпойнтах (SEC-05). None → проверка пропускается
            (fail-open, dev/CI без секрета).

    Returns:
        Tornado Application
    """
    debug = os.getenv("DEBUG", "false").lower() == "true"
    return tornado.web.Application([
        # Health-depth: /live (без БД), /ready (db critical),
        # /health (DB-only, docker healthcheck back-compat).
        (r"/live", LivenessHandler),
        (r"/ready", ReadinessHandler, dict(checks=[db_check(db)])),
        (r"/health", HealthHandler, dict(db=db)),

        # =================================================================
        # Prometheus scrape endpoint. См. user_api/main.py.
        # =================================================================
        (r"/metrics", MetricsHandler),

        # =================================================================
        # Telemetry endpoints
        # =================================================================
        (r"/api/v1/telemetry", TelemetryBatchHandler, dict(db=db)),
        (r"/api/v1/telemetry/list", TelemetryListHandler, dict(db=db)),

        # =================================================================
        # Security Events endpoints
        # =================================================================
        (r"/api/v1/events/security", SecurityEventListHandler, dict(db=db)),
        (r"/api/v1/events/security/([0-9]+)", SecurityEventDetailHandler, dict(db=db)),

        # =================================================================
        # Command History endpoints
        # =================================================================
        (r"/api/v1/commands/history", CommandHistoryHandler, dict(db=db)),

        # =================================================================
        # Analytics endpoints
        # =================================================================
        (r"/api/v1/analytics/sensors", SensorAnalyticsHandler, dict(db=db)),
        (r"/api/v1/analytics/lockers/([a-f0-9\-]+)/summary", LockerSummaryHandler, dict(db=db)),

        # =================================================================
        # Media endpoints (фото и видео)
        # =================================================================
        (r"/api/v1/media", MediaListHandler, dict(db=db)),
        (r"/api/v1/media/([0-9]+)", MediaDetailHandler, dict(db=db)),
        (r"/api/v1/events/security/([0-9]+)/media", MediaListByEventHandler, dict(db=db)),

        # =================================================================
        # Internal endpoints (закрыты снаружи через Apache <Location /internal>).
        # Используются продьюсерами locker_api для записи событий, телеметрии,
        # истории команд и медиа без X-Remote-User.
        # =================================================================
        (r"/internal/api/v1/events/security", InternalSecurityEventHandler, dict(db=db)),
        (r"/internal/api/v1/telemetry", InternalTelemetryHandler, dict(db=db)),
        (r"/internal/api/v1/commands/history", InternalCommandHistoryHandler, dict(db=db)),
        (r"/internal/api/v1/media", InternalMediaUploadHandler, dict(db=db)),

    ], debug=debug, internal_history_hmac_key=internal_history_hmac_key)


async def main() -> None:
    """Главная функция запуска сервиса"""
    configure_logging(os.getenv("SERVICE_NAME", "history_api"))
    logger.info("Starting History API...")

    # пароль БД читается из Docker secret или env-fallback.
    db_password = read_secret(
        "postgres_history_password", env_var="DB_PASSWORD", default="postgres"
    )
    # Конфигурация БД из переменных окружения
    db_config = PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=db_password,
        database=os.getenv("DB_NAME", "scud_history"),
    )

    # Ожидание готовности БД с retry — на старте контейнера postgres ещё может быть unavailable
    db = await wait_for_db(db_config)
    logger.info(f"Connected to database: {db_config.database}@{db_config.host}")

    # Pull-based DB-pool-saturation метрики: семплируются на scrape /metrics.
    register_pool(db.pool_name, db.get_pool_info)

    # Запуск фоновых сервисов — создаём raньше app.listen, чтобы:
    # 1. Прогреть партиции `sensor_telemetry` (текущий месяц + lookahead)
    #    ДО приёма первого запроса — иначе ранние INSERTs полетят в DEFAULT-
    #    партицию и потеряют партиционную подрезку.
    # 2. Не зависеть на bootstrap'е от RetentionService loop'а — `_create_future_partitions`
    #    идемпотентен и вызывается напрямую.
    aggregation_interval = int(os.getenv("AGGREGATION_INTERVAL_SECONDS", "300"))
    retention_interval = int(os.getenv("RETENTION_INTERVAL_SECONDS", "3600"))
    retention_months = int(os.getenv("RETENTION_MONTHS", "24"))

    aggregation_service = AggregationService(db)
    retention_service = RetentionService(db, retention_months=retention_months)

    try:
        await retention_service._create_future_partitions()
    except Exception:
        logger.exception("Failed to prime sensor_telemetry partitions on startup")

    # HMAC-ключ для проверки подписи на internal write-эндпойнтах (SEC-05).
    # ОБЯЗАН совпадать со значением в locker_api и auth_api (продьюсеры).
    # Если не задан — endpoint'ы работают без проверки подписи (dev/CI).
    internal_history_hmac_key = read_secret(
        "internal_history_hmac_key",
        env_var="INTERNAL_HISTORY_HMAC_KEY",
        default="",
        required=is_production(),
    ) or None
    if internal_history_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_HISTORY_HMAC_KEY is not set — internal "
            "/internal/api/v1/{events/security,telemetry,media,commands/history} "
            "requests are accepted UNSIGNED. Set the same value in history_api, "
            "locker_api and auth_api before production deploy.",
        )

    # Создание Tornado приложения
    app = make_app(db, internal_history_hmac_key=internal_history_hmac_key)

    # Настройка порта
    port = int(os.getenv("APP_PORT", 8002))
    app.listen(port, xheaders=True)
    logger.info(f"History API listening on port {port}")

    logger.info("Service started successfully")
    logger.info("  - Telemetry: POST/GET /api/v1/telemetry")
    logger.info("  - Security Events: POST/GET /api/v1/events/security")
    logger.info("  - Command History: POST/GET /api/v1/commands/history (idempotent by command_id)")
    logger.info("  - Analytics: GET /api/v1/analytics/sensors, /api/v1/analytics/lockers/:id/summary")
    logger.info("  - Media: GET /api/v1/media, GET /api/v1/media/{id}, GET /api/v1/events/security/{id}/media")
    logger.info("  - Internal: POST /internal/api/v1/{events/security,telemetry,commands/history,media}")
    logger.info(f"  - Database: {db_config.database}@{db_config.host}")

    aggregation_task = asyncio.create_task(
        aggregation_service.start(interval=aggregation_interval)
    )
    retention_task = asyncio.create_task(
        retention_service.start(interval=retention_interval)
    )

    logger.info("Background services started:")
    logger.info(f"  - AggregationService ({aggregation_interval}s interval)")
    logger.info(f"  - RetentionService ({retention_interval}s interval, {retention_months} months retention)")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    logger.info("History API started, waiting for shutdown signal")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        retention_service.stop()
        aggregation_service.stop()
        await retention_task
        await aggregation_task
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
