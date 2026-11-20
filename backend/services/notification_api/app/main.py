import asyncio
from logging import getLogger
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
from shared.enums import NotificationChannel
from shared.http import RetryHTTPClient
from shared.metrics import register_pool
from shared.metrics.handler import MetricsHandler
from shared.observability import configure_logging
from shared.utils import is_production, read_secret

from .handlers.internal_notification import (
    InternalNotificationDetailHandler,
    InternalNotificationListHandler,
)
from .handlers.internal_otp import InternalOtpSendHandler
from .handlers.notification import NotificationDetailHandler, NotificationListHandler
from .notification_data.db.repositories.bot_poller_state import BotPollerStateRepository
from .notification_data.db.repositories.outbound_max import OutboundMaxRepository
from .services.max_bot_client import MaxBotClient
from .services.max_bot_poller import MaxBotPoller
from .services.scheduler import NotificationScheduler
from .services.sms_gateway_client import SmsGatewayClient
from .services.transport import NotificationTransport
from .services.user_api_binding_client import UserApiBindingClient

logger = getLogger(__name__)

SMS_GATEWAY_TIMEOUT_ENV = "SMS_GATEWAY_TIMEOUT_SECONDS"
DEFAULT_SMS_GATEWAY_TIMEOUT_SECONDS = 25.0
SMS_MAX_QUEUE_WAIT_ENV = "SMS_MAX_QUEUE_WAIT_SECONDS"
DEFAULT_SMS_MAX_QUEUE_WAIT_SECONDS = 10.0
MAX_SEND_TIMEOUT_ENV = "MAX_SEND_TIMEOUT_SECONDS"
DEFAULT_MAX_SEND_TIMEOUT_SECONDS = 8.0
MAX_POLL_TIMEOUT_SECONDS = 35.0


def build_sms_http() -> RetryHTTPClient:
    # max_attempts=1 — SMS не идемпотентен: RetryHTTPClient ретраит GET по
    # умолчанию, а повтор после timeout/5xx означает дубликат SMS и обход
    # пейсера. redact_url=True — в query-строке пароль шлюза
    # и текст OTP, без редактирования они утекут в WARNING-логи ретраев.
    # Шлюз отвечает от ~3 с до ~15 с (Commit Timeout), поэтому таймаут длинный.
    timeout = float(
        os.getenv(SMS_GATEWAY_TIMEOUT_ENV, str(DEFAULT_SMS_GATEWAY_TIMEOUT_SECONDS))
    )
    return RetryHTTPClient(request_timeout=timeout, max_attempts=1, redact_url=True)


def read_sms_max_queue_wait() -> float:
    return float(
        os.getenv(SMS_MAX_QUEUE_WAIT_ENV, str(DEFAULT_SMS_MAX_QUEUE_WAIT_SECONDS))
    )


def build_max_send_http() -> RetryHTTPClient:
    # Путь доставки OTP: POST не ретраится на таймауте/5xx (дубль кода),
    # повторяется только connection-refused.
    timeout = float(
        os.getenv(MAX_SEND_TIMEOUT_ENV, str(DEFAULT_MAX_SEND_TIMEOUT_SECONDS))
    )
    return RetryHTTPClient(request_timeout=timeout)


def build_max_poll_http() -> RetryHTTPClient:
    # Long-polling GET /updates подвешивает соединение до 30 с.
    return RetryHTTPClient(request_timeout=MAX_POLL_TIMEOUT_SECONDS)


def make_app(
    db: AsyncPostgres,
    *,
    transports: dict[NotificationChannel, NotificationTransport] | None = None,
    internal_otp_hmac_key: bytes | None = None,
) -> tornado.web.Application:
    debug = os.getenv("DEBUG", "false").lower() == "true"
    return tornado.web.Application(
        [
            # Health-depth: /live (без БД), /ready (db critical),
            # /health (DB-only, docker healthcheck back-compat).
            (r"/live", LivenessHandler),
            (r"/ready", ReadinessHandler, dict(checks=[db_check(db)])),
            (r"/health", HealthHandler, dict(db=db)),

            # Prometheus scrape endpoint. См. user_api/main.py.
            (r"/metrics", MetricsHandler),

            # Коллекция уведомлений (plural)
            (r"/api/v1/notifications", NotificationListHandler, dict(db=db)),

            # Конкретное уведомление (singular)
            (r"/api/v1/notification/([0-9a-f-]+)", NotificationDetailHandler, dict(db=db)),

            # Internal: системные уведомления (state_monitor → notification_client).
            # Закрыто снаружи через Apache <Location /internal> Require all denied.
            (r"/internal/api/v1/notifications", InternalNotificationListHandler, dict(db=db)),
            (r"/internal/api/v1/notification/([0-9a-f-]+)", InternalNotificationDetailHandler, dict(db=db)),

            # Internal: отправка OTP-кодов в Max-бот по запросу auth_api.
            (r"/internal/api/v1/otp/send", InternalOtpSendHandler, dict(db=db)),
        ],
        debug=debug,
        transports=transports or {},
        internal_otp_hmac_key=internal_otp_hmac_key,
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
    configure_logging(os.getenv("SERVICE_NAME", "notification_api"))
    # пароль БД читается из Docker secret или env-fallback.
    db_password = read_secret(
        "postgres_notification_password", env_var="DB_PASSWORD", default="postgres"
    )
    config = PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "postgres"),
        password=db_password,
        database=os.getenv("DB_NAME", "scud_notifications"),
    )

    db = await wait_for_db(config)

    # Pull-based DB-pool-saturation метрики: семплируются на scrape /metrics.
    register_pool(db.pool_name, db.get_pool_info)

    scheduler = NotificationScheduler(db)

    # Max-бот: токен и URL читаются из Docker secret/env. Если токен пуст —
    # клиент не инициализируется, handler /internal/api/v1/otp/send
    # отвечает 200 queued_until_binding и оператор по логу видит, что
    # стартап в degraded-режиме (dev).
    max_token = read_secret("max_bot_token", env_var="MAX_BOT_TOKEN", default="") or ""
    max_api_url = os.getenv("MAX_BOT_API_URL", "https://platform-api.max.ru")
    max_client: MaxBotClient | None
    if max_token:
        max_client = MaxBotClient(
            http=build_max_poll_http(),
            send_http=build_max_send_http(),
            base_url=max_api_url,
            token=max_token,
        )
    else:
        logger.warning(
            "MAX_BOT_TOKEN is empty: Max-бот не подключён. OTP-коды будут "
            "оседать в outbox со статусом queued_until_binding. "
            "Допустимо только для локальной разработки.",
        )
        max_client = None

    transports: dict[NotificationChannel, NotificationTransport] = {}
    if max_client is not None:
        transports[NotificationChannel.MAX] = max_client

    sms_url = os.getenv("SMS_GATEWAY_URL", "").strip()
    sms_user = read_secret("sms_api_user", env_var="SMS_API_USER", default="") or ""
    sms_pass = read_secret("sms_api_password", env_var="SMS_API_PASSWORD", default="") or ""
    if sms_url and sms_user and sms_pass:
        sms_interval = float(os.getenv("SMS_MIN_INTERVAL_SECONDS", "10"))
        transports[NotificationChannel.SMS] = SmsGatewayClient(
            http=build_sms_http(), base_url=sms_url, account=sms_user, password=sms_pass,
            min_interval_s=sms_interval, max_queue_wait_s=read_sms_max_queue_wait(),
        )
        logger.info("SMS transport configured: %s", sms_url)
    else:
        logger.warning(
            "SMS transport not configured (SMS_GATEWAY_URL/SMS_API_USER/SMS_API_PASSWORD). "
            "OTP will use MAX or queue. Expected on dev/CI.",
        )

    # HMAC-ключ для подписи запросов auth_api → /internal/api/v1/otp/send.
    # Если не задан — endpoint работает без проверки подписи (dev-режим).
    otp_hmac_str = read_secret(
        "internal_otp_hmac_key", env_var="INTERNAL_OTP_HMAC_KEY", default="",
        required=is_production(),
    ) or ""
    internal_otp_hmac_key: bytes | None = (
        otp_hmac_str.encode("utf-8") if otp_hmac_str else None
    )
    if internal_otp_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_OTP_HMAC_KEY is not set — /internal/api/v1/otp/send "
            "requests are accepted UNSIGNED. Set the same value in auth_api and "
            "notification_api before production deploy.",
        )

    # HMAC-ключ для подписи запросов в user_api /internal/user/by-invite.
    max_binding_hmac_str = read_secret(
        "internal_max_binding_hmac_key",
        env_var="INTERNAL_MAX_BINDING_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    max_binding_hmac_key: bytes | None = (
        max_binding_hmac_str.encode("utf-8") if max_binding_hmac_str else None
    )
    if max_binding_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_MAX_BINDING_HMAC_KEY is not set — /internal/user/by-invite "
            "requests will be sent UNSIGNED. Set the same value in user_api and "
            "notification_api before production deploy.",
        )

    app = make_app(
        db,
        transports=transports,
        internal_otp_hmac_key=internal_otp_hmac_key,
    )
    port = int(os.getenv("APP_PORT", 8005))
    app.listen(port, xheaders=True)
    logger.info(f"Notification API started on port {port}")

    check_interval = int(os.getenv("SCHEDULER_INTERVAL_SECONDS", "30"))
    scheduler_task = asyncio.create_task(
        scheduler.start(check_interval=check_interval)
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    poller_task: asyncio.Task[None] | None = None
    if max_client is not None:
        user_api_url = os.getenv("USER_API_URL", "http://user_api:8000")
        binding_client = UserApiBindingClient(
            user_api_url, hmac_key=max_binding_hmac_key,
        )
        poller = MaxBotPoller(
            max_client=max_client,
            binding_client=binding_client,
            state_repo=BotPollerStateRepository(db),
            outbox_repo=OutboundMaxRepository(db),
        )
        poller_task = asyncio.create_task(poller.run(stop_event))
        logger.info("Background services started: Notification Scheduler + Max Bot Poller")
    else:
        logger.info(
            "Background services started: Notification Scheduler (Max Bot Poller disabled — MAX_BOT_TOKEN empty)",
        )

    logger.info("Notification API started, waiting for shutdown signal")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        scheduler.stop()
        await scheduler_task
        if poller_task is not None:
            await poller_task
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
