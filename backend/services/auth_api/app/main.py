import asyncio
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
)
from shared.database import AsyncPostgres, PostgresConfig
from shared.http import RetryHTTPClient
from shared.metrics import register_pool
from shared.metrics.handler import MetricsHandler
from shared.observability import configure_logging
from shared.utils import is_production, read_secret

from .auth_data.db.repositories.otp_repository import OtpRepository
from .auth_data.db.repositories.rate_limit_repository import RateLimitRepository
from .auth_data.db.repositories.step_up import StepUpChallengeRepository
from .auth_data.db.repositories.token_repository import TokenRepository
from .handlers.access import AccessHandler
from .handlers.login import LoginHandler
from .handlers.logout import LogoutHandler
from .handlers.revoke_user import RevokeUserTokensHandler
from .handlers.session_revoke import SessionRevokeHandler
from .handlers.step_up import StepUpIssueHandler, StepUpVerifyHandler
from .handlers.validate import ValidateHandler
from .handlers.verify import VerifyHandler
from .services.notification_client import NotificationClient
from .services.security_event_producer import SecurityEventProducer
from .services.user_api_client import UserApiClient

logger = logging.getLogger(__name__)

OTP_DELIVERY_TIMEOUT_ENV = "OTP_DELIVERY_TIMEOUT_SECONDS"
DEFAULT_OTP_DELIVERY_TIMEOUT_SECONDS = 45.0


def build_notification_http() -> RetryHTTPClient:
    # Худший случай notification_api: очередь пейсера (10 с) + шлюз (25 с) +
    # fallback в MAX (8 с) = 43 с; таймаут обязан быть больше, иначе логин даёт
    # 503 при реально доставленном SMS. POST без idempotent=True на таймауте
    # не ретраится (дубль кода/SMS).
    timeout = float(
        os.getenv(OTP_DELIVERY_TIMEOUT_ENV, str(DEFAULT_OTP_DELIVERY_TIMEOUT_SECONDS))
    )
    return RetryHTTPClient(request_timeout=timeout)


def make_app(db: AsyncPostgres, settings: dict[str, Any]) -> tornado.web.Application:
    handler_kwargs = dict(db=db)
    return tornado.web.Application(
        [
            # Health-depth: /live (без БД), /ready (db critical),
            # /health (DB-only, docker healthcheck back-compat).
            (r"/live", LivenessHandler),
            (r"/ready", ReadinessHandler, dict(checks=[db_check(db)])),
            (r"/health", HealthHandler, handler_kwargs),

            # Prometheus scrape endpoint. См. user_api/main.py.
            (r"/metrics", MetricsHandler),

            # Internal: вызывается Apache Lua для проверки JWT
            (r"/auth/validate", ValidateHandler, handler_kwargs),

            # Public: auth endpoints для пользователей
            (r"/api/v1/auth/login", LoginHandler, handler_kwargs),
            (r"/api/v1/auth/verify", VerifyHandler, handler_kwargs),
            (r"/api/v1/auth/access", AccessHandler, handler_kwargs),
            (r"/api/v1/auth/logout", LogoutHandler, handler_kwargs),
            (r"/api/v1/auth/users/(\d+)/tokens", RevokeUserTokensHandler, handler_kwargs),

            # Internal step-up OTP endpoints (закрыты Apache <Location /internal>).
            (r"/internal/api/v1/step-up/issue", StepUpIssueHandler, handler_kwargs),
            (r"/internal/api/v1/step-up/verify", StepUpVerifyHandler, handler_kwargs),

            # Internal: user_api дёргает после смены пароля / 2FA, чтобы отозвать
            # все живые сессии пользователя (SEC-14). Закрыт Apache снаружи.
            (r"/internal/api/v1/sessions/revoke", SessionRevokeHandler, handler_kwargs),
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
                logger.error(f"Failed to connect to DB after {max_attempts} attempts.")
                raise
            logger.warning(f"DB not ready ({e}); retrying in {delay:.1f}s [{attempts}/{max_attempts}]...")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 10.0)


_DEFAULT_JWT_SECRET = "change-me-in-production-must-be-at-least-32-bytes-long"


def _warn_if_default_jwt_secret(settings: dict[str, Any], log: logging.Logger) -> None:
    if settings.get("jwt_secret") == _DEFAULT_JWT_SECRET:
        log.warning(
            "[SECURITY] JWT_SECRET uses default placeholder — regenerate via "
            "`openssl rand -hex 32` and set in .env before production deploy"
        )


_CLEANUP_INTERVAL_SECONDS = 3600
# Удержание строк login_rate_limit: после этого окна строка устарела и не
# влияет на rate-limit (check_and_record всё равно перезапустил бы окно).
# Достаточно нескольких окон запаса; берём тот же дефолт, что и интервал.
_RATE_LIMIT_RETENTION_SECONDS = 3600


async def cleanup_loop(db: AsyncPostgres) -> None:
    """Периодическая очистка устаревших записей (каждые 60 мин)."""
    token_repo = TokenRepository(db)
    otp_repo = OtpRepository(db)
    step_up_repo = StepUpChallengeRepository(db)
    rate_limit_repo = RateLimitRepository(db)
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
        try:
            await token_repo.delete_expired()
            await otp_repo.delete_expired()
            await step_up_repo.delete_expired()
            await rate_limit_repo.delete_stale(_RATE_LIMIT_RETENTION_SECONDS)
            logger.info("Expired auth records cleaned up")
        except Exception as e:
            logger.error(f"Cleanup error: {e}")


async def main() -> None:
    configure_logging(os.getenv("SERVICE_NAME", "auth_api"))

    # пароль БД читается из Docker secret или env-fallback.
    db_password = read_secret(
        "postgres_auth_password", env_var="DB_PASSWORD", default="postgres"
    )
    config = PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "postgres"),
        password=db_password,
        database=os.getenv("DB_NAME", "scud_auth"),
    )

    db = await wait_for_db(config)

    # Pull-based DB-pool-saturation метрики: семплируются на scrape /metrics.
    register_pool(db.pool_name, db.get_pool_info)

    user_api_url = os.getenv("USER_API_URL", "http://user_api:8000")
    notification_api_url = os.getenv(
        "NOTIFICATION_API_URL", "http://notification_api:8005",
    )
    history_api_url = os.getenv("HISTORY_API_URL", "http://history_api:8002")
    # Общий секрет для подписи внутренних запросов auth_api → user_api
    # ``/internal/auth/check``. Если переменная не установлена — клиент будет
    # отправлять запросы без подписи (для запуска без .env в dev). user_api
    # в таком же режиме пропустит их, но в production ключ обязателен.
    # читаем сначала из /run/secrets/internal_auth_hmac_key, иначе env.
    internal_hmac_key_str = read_secret(
        "internal_auth_hmac_key",
        env_var="INTERNAL_AUTH_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    internal_hmac_key: bytes | None = (
        internal_hmac_key_str.encode("utf-8") if internal_hmac_key_str else None
    )
    if internal_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_AUTH_HMAC_KEY is not set — /internal/auth/check "
            "requests will be sent UNSIGNED. Set the same value in auth_api and "
            "user_api before production deploy."
        )

    # Отдельный HMAC-ключ для запросов в notification_api /internal/api/v1/otp/send.
    # Компрометация одного ключа не даёт доступа к другому inter-service path.
    otp_hmac_key_str = read_secret(
        "internal_otp_hmac_key",
        env_var="INTERNAL_OTP_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    otp_hmac_key: bytes | None = (
        otp_hmac_key_str.encode("utf-8") if otp_hmac_key_str else None
    )
    if otp_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_OTP_HMAC_KEY is not set — /internal/api/v1/otp/send "
            "requests will be sent UNSIGNED. Set the same value in auth_api and "
            "notification_api before production deploy."
        )

    # Отдельный HMAC-ключ для step-up endpoints: подписывает входящие
    # /internal/api/v1/step-up/{issue,verify} от locker_api и исходящие
    # /internal/user/{id}/max-context в user_api. Разделение per-domain
    # снижает blast radius при компрометации одного ключа.
    step_up_hmac_key_str = read_secret(
        "internal_step_up_hmac_key",
        env_var="INTERNAL_STEP_UP_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    step_up_hmac_key: bytes | None = (
        step_up_hmac_key_str.encode("utf-8") if step_up_hmac_key_str else None
    )
    if step_up_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_STEP_UP_HMAC_KEY is not set — "
            "/internal/api/v1/step-up/* requests will be processed UNSIGNED. "
            "Set the same value in auth_api and locker_api before production deploy."
        )

    # Отдельный HMAC-ключ для PII-read запросов в user_api:
    # /internal/user/by-phone и /internal/user/{id}. Разделён per-domain —
    # компрометация не открывает /internal/auth/check (SEC-10).
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
            "/internal/user/{by-phone,by-id} requests will be sent UNSIGNED. "
            "Set the same value in auth_api, user_api and reports_api before "
            "production deploy."
        )

    # Отдельный HMAC-ключ для проверки входящих /internal/api/v1/sessions/revoke
    # от user_api: после смены пароля / 2FA user_api просит отозвать все живые
    # сессии пользователя. Разделён per-domain — компрометация не открывает
    # прочие inter-service path (SEC-14).
    session_revoke_hmac_key_str = read_secret(
        "internal_session_revoke_hmac_key",
        env_var="INTERNAL_SESSION_REVOKE_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    session_revoke_hmac_key: bytes | None = (
        session_revoke_hmac_key_str.encode("utf-8")
        if session_revoke_hmac_key_str else None
    )
    if session_revoke_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_SESSION_REVOKE_HMAC_KEY is not set — "
            "/internal/api/v1/sessions/revoke will accept UNSIGNED requests. "
            "Set the same value in auth_api and user_api before production deploy."
        )

    # Общий с history_api ключ для подписи /internal/api/v1/events/security
    # (аудит step-up 2FA: issued/verified/failed). SEC-05.
    history_hmac_key_str = read_secret(
        "internal_history_hmac_key",
        env_var="INTERNAL_HISTORY_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    history_hmac_key: bytes | None = (
        history_hmac_key_str.encode("utf-8") if history_hmac_key_str else None
    )
    if history_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_HISTORY_HMAC_KEY is not set — "
            "/internal/api/v1/events/security audit requests will be sent "
            "UNSIGNED. Set the same value in history_api, locker_api and "
            "auth_api before production deploy."
        )

    # JWT-секрет читается из Docker secret или env-fallback.
    jwt_secret = read_secret(
        "jwt_secret",
        env_var="JWT_SECRET",
        default="dev-secret-change-in-production",
        required=is_production(),
    )
    app_settings = {
        "jwt_secret": jwt_secret,
        "user_api_url": user_api_url,
        "user_api_client": UserApiClient(
            user_api_url,
            hmac_key=internal_hmac_key,
            step_up_hmac_key=step_up_hmac_key,
            user_read_hmac_key=user_read_hmac_key,
        ),
        "notification_api_url": notification_api_url,
        "notification_client": NotificationClient(
            notification_api_url, http=build_notification_http(), hmac_key=otp_hmac_key,
        ),
        "security_event_producer": SecurityEventProducer(
            history_api_url, hmac_key=history_hmac_key,
        ),
        "internal_step_up_hmac_key": step_up_hmac_key,
        "internal_session_revoke_hmac_key": session_revoke_hmac_key,
        "otp_ttl_seconds": int(os.getenv("OTP_TTL_SECONDS", "300")),
        "otp_max_attempts": int(os.getenv("OTP_MAX_ATTEMPTS", "3")),
        "access_token_ttl_hours": int(os.getenv("ACCESS_TOKEN_TTL_HOURS", "720")),
        "secure_cookies": os.getenv("SECURE_COOKIES", "true").lower() == "true",
        "login_rate_limit": int(os.getenv("LOGIN_RATE_LIMIT", "10")),
        "login_rate_window_seconds": int(os.getenv("LOGIN_RATE_WINDOW_SECONDS", "60")),
        "verify_rate_limit": int(os.getenv("VERIFY_RATE_LIMIT", "10")),
        "verify_rate_window_seconds": int(os.getenv("VERIFY_RATE_WINDOW_SECONDS", "60")),
    }

    _warn_if_default_jwt_secret(app_settings, logger)

    app = make_app(db, app_settings)
    port = int(os.getenv("APP_PORT", 8003))
    # xheaders=True заставляет Tornado читать X-Real-IP / X-Forwarded-For как
    # request.remote_ip. Без этого rate-limit работал по IP gateway-контейнера
    # (один IP на всех клиентов) — фактически глобальный, а не per-client.
    # Apache gateway проставляет `RequestHeader set X-Real-IP "%{REMOTE_ADDR}s"`.
    app.listen(port, xheaders=True)
    logger.info(f"Auth API started on port {port}")

    cleanup_task = asyncio.create_task(cleanup_loop(db))

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    logger.info("Auth API started, waiting for shutdown signal")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        cleanup_task.cancel()
        # Дожидаемся завершения cleanup_loop, чтобы активная транзакция
        # token_repo.delete_expired() / otp_repo.delete_expired() не оборвалась
        # на половине, когда db.close() закроет пул соединений.
        await asyncio.gather(cleanup_task, return_exceptions=True)
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
