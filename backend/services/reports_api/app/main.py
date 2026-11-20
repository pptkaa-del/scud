import asyncio
from logging import getLogger
import os
from pathlib import Path
import signal

import psycopg2
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

from .handlers.report_detail import ReportDetailHandler
from .handlers.report_download import ReportDownloadHandler
from .handlers.reports import ReportListHandler
from .reports.base import ReportGenerator
from .reports.registry import build_registry
from .reports_data.db.repositories.report_job import ReportJobRepository
from .services.history_client import HistoryReportClient
from .services.user_client import UserApiClient
from .services.worker import ReportWorker

logger = getLogger(__name__)


def make_app(
    db: AsyncPostgres, *, registry: dict[str, ReportGenerator]
) -> tornado.web.Application:
    debug = os.getenv("DEBUG", "false").lower() == "true"
    return tornado.web.Application(
        [
            # Health-depth: /live (без БД), /ready (db critical),
            # /health (DB-only, docker healthcheck back-compat).
            (r"/live", LivenessHandler),
            (r"/ready", ReadinessHandler, dict(checks=[db_check(db)])),
            (r"/health", HealthHandler, dict(db=db)),

            # Synchronous CRUD: постановка задачи, список и poll одной задачи.
            (r"/api/v1/reports", ReportListHandler, dict(db=db, registry=registry)),
            # ВАЖНО: /download регистрируется ДО detail-роута, чтобы Tornado
            # не сматчил detail первым (явный порядок, хоть $-якорь и спасает).
            (r"/api/v1/report/([0-9a-fA-F-]+)/download", ReportDownloadHandler, dict(db=db)),
            (r"/api/v1/report/([0-9a-fA-F-]+)", ReportDetailHandler, dict(db=db)),

            # Prometheus scrape endpoint. См. notification_api/main.py.
            (r"/metrics", MetricsHandler),
        ],
        debug=debug,
    )


async def wait_for_db(config: PostgresConfig) -> AsyncPostgres:
    """Ожидание готовности БД с ретраями и экспоненциальной задержкой"""
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


async def main() -> None:
    configure_logging(os.getenv("SERVICE_NAME", "reports_api"))
    # пароль БД читается из Docker secret или env-fallback.
    db_password = read_secret(
        "postgres_reports_password", env_var="DB_PASSWORD", default="postgres"
    )
    config = PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "admin"),
        password=db_password,
        database=os.getenv("DB_NAME", "scud_reports"),
    )

    db = await wait_for_db(config)

    # Pull-based DB-pool-saturation метрики: семплируются на scrape /metrics.
    register_pool(db.pool_name, db.get_pool_info)

    history_client = HistoryReportClient(
        os.getenv("HISTORY_API_URL", "http://history_api:8002")
    )
    # Отдельный HMAC-ключ для PII-read запросов /internal/users/names в user_api.
    # Разделён per-domain — компрометация не открывает другие inter-service
    # каналы (SEC-10). None → dev/CI fail-open.
    user_read_hmac_key_str = read_secret(
        "internal_user_read_hmac_key",
        env_var="INTERNAL_USER_READ_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    user_read_hmac_key: bytes | None = (
        user_read_hmac_key_str.encode("utf-8") if user_read_hmac_key_str else None
    )
    if user_read_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_USER_READ_HMAC_KEY is not set — "
            "/internal/users/names requests will be sent UNSIGNED. Set the "
            "same value in reports_api and user_api before production deploy."
        )
    user_client = UserApiClient(
        os.getenv("USER_API_URL", "http://user_api:8000"),
        hmac_key=user_read_hmac_key,
    )
    registry = build_registry(history_client, user_client)

    app = make_app(db, registry=registry)
    port = int(os.getenv("APP_PORT", "8006"))
    app.listen(port, xheaders=True)
    logger.info(f"Reports API started on port {port}")

    storage_path = Path(os.getenv("REPORTS_STORAGE_PATH", "/data/reports"))
    ttl = int(os.getenv("REPORT_LINK_TTL_SECONDS", "86400"))
    interval = float(os.getenv("REPORT_WORKER_INTERVAL_S", "5"))
    max_attempts = int(os.getenv("REPORT_MAX_ATTEMPTS", "3"))
    worker = ReportWorker(
        repo=ReportJobRepository(db),
        registry=registry,
        storage_path=storage_path,
        ttl_seconds=ttl,
        max_attempts=max_attempts,
        interval_s=interval,
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    worker_task = asyncio.create_task(worker.run(stop_event))

    logger.info("Reports API started, waiting for shutdown signal")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        stop_event.set()
        await worker_task
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
