"""
POST /api/v1/auth/login

Шаг 1 авторизации: проверка телефона и пароля.

Если require_2fa=true  → отправить OTP-код по SMS (fallback — Max-бот), вернуть 200 {"message": "OTP has been sent"}
Если require_2fa=false → установить сессионный cookie, вернуть 200 {"message": "Authentication successful"}
Если учётные данные неверны → 401
Если OTP не удалось доставить ни одним каналом → 503 (сервис доставки недоступен)

Body: {"phone": "...", "password": "..."}
"""
from datetime import UTC, datetime, timedelta
import logging
import secrets

from app.auth_data.db.repositories.otp_repository import OtpRepository
from app.auth_data.db.repositories.rate_limit_repository import RateLimitRepository
from app.auth_data.db.repositories.token_repository import TokenRepository
from app.services.notification_client import NotificationClient, OtpDeliveryStatus
from app.services.otp import generate_otp, hash_otp
from app.services.user_api_client import UserApiClient
from shared.enums import LoginOutcome, OtpOutcome, OtpStage
from shared.http import UpstreamUnavailableError
from shared.metrics import SERVICE_NAME, login_attempts_total, otp_total
from shared.utils import hash_password, hash_token

from .base import BaseHandler

logger = logging.getLogger(__name__)

OTP_DELIVERY_UNAVAILABLE_DETAIL = (
    "Не удалось отправить код подтверждения. "
    "Попробуйте позже или обратитесь к администратору."
)
AUTH_BACKEND_UNAVAILABLE_DETAIL = (
    "Сервис авторизации временно недоступен. Попробуйте позже."
)


class LoginHandler(BaseHandler):
    """
    Обрабатывает первый шаг входа: проверка пароля.

    Если require_2fa=true: генерирует OTP и шлёт его через notification_api
    (SMS-primary, Max-бот — fallback). Телефон у пользователя есть всегда,
    поэтому `queued_until_binding` (ни один канал не доставил код) отвечает
    503, как и явный сбой notification_api.

    Если require_2fa=false: выдаёт JWT + access token напрямую.
    При неверных данных — 401.
    """

    async def post(self) -> None:
        rate_limit = int(self.settings.get("login_rate_limit", 10))
        rate_window = int(self.settings.get("login_rate_window_seconds", 60))
        ip = self.request.remote_ip
        if ip is None:
            logger.warning(
                "remote_ip not populated for login attempt — check xheaders / gateway config; "
                "falling back to shared 'unknown' rate-limit bucket"
            )
            ip = "unknown"
        rate_repo = RateLimitRepository(self.db)
        if not await rate_repo.check_and_record(ip, rate_limit, rate_window):
            login_attempts_total.labels(
                service=SERVICE_NAME, outcome=LoginOutcome.RATE_LIMITED,
            ).inc()
            self.write_too_many_requests("Too many login attempts, try again later")
            return

        body = self.get_json_body()
        phone = body.get("phone", "").strip()
        password = body.get("password", "")

        if not phone or not password:
            self.write_validation_error("phone and password are required")
            return

        try:
            result = await self._check_credentials(phone, password)
        except UpstreamUnavailableError:
            login_attempts_total.labels(
                service=SERVICE_NAME, outcome=LoginOutcome.UPSTREAM_UNAVAILABLE,
            ).inc()
            self.write_service_unavailable(AUTH_BACKEND_UNAVAILABLE_DETAIL)
            return

        if result is None:
            login_attempts_total.labels(
                service=SERVICE_NAME, outcome=LoginOutcome.BAD_CREDENTIAL,
            ).inc()
            self.write_unauthorized("Invalid phone number or password")
            return

        user_id, require_2fa, name, role, max_user_id = result
        if not require_2fa:
            # 2FA отключена — устанавливаем сессионный cookie сразу.
            access_token = await self._issue_access_token(user_id)
            self._set_access_cookie(access_token)
            login_attempts_total.labels(
                service=SERVICE_NAME, outcome=LoginOutcome.SUCCESS_NO_2FA,
            ).inc()
            self.write_json({"message": "Authentication successful", "name": name, "role": role})
            return

        # 2FA включена — генерим OTP, сохраняем хэш в БД, отправляем по SMS/Max.
        try:
            status = await self._create_and_send_otp(
                phone=phone, user_id=user_id, max_user_id=max_user_id,
            )
        except UpstreamUnavailableError:
            otp_total.labels(
                service=SERVICE_NAME, stage=OtpStage.ISSUE,
                outcome=OtpOutcome.DELIVERY_UNAVAILABLE,
            ).inc()
            self.write_service_unavailable(OTP_DELIVERY_UNAVAILABLE_DETAIL)
            return

        if status == OtpDeliveryStatus.QUEUED_UNTIL_BINDING:
            # SMS-primary: телефон есть всегда, поэтому queued означает, что
            # ни SMS-шлюз, ни MAX недоступны. Вход временно невозможен.
            otp_total.labels(
                service=SERVICE_NAME, stage=OtpStage.ISSUE,
                outcome=OtpOutcome.DELIVERY_UNAVAILABLE,
            ).inc()
            self.write_service_unavailable(OTP_DELIVERY_UNAVAILABLE_DETAIL)
            return

        otp_total.labels(
            service=SERVICE_NAME, stage=OtpStage.ISSUE, outcome=OtpOutcome.SENT,
        ).inc()
        self.write_json({"message": "OTP has been sent"})

    async def _check_credentials(
        self, phone: str, password: str,
    ) -> tuple[int, bool, str, str, int | None] | None:
        """
        Проверить телефон + пароль через user_api /internal/auth/check.

        Returns:
            (user_id, require_2fa, name, role, max_user_id) при успехе,
            None при неверных кредах.

        Raises:
            UpstreamUnavailableError если user_api недоступен.
        """
        client: UserApiClient = self.settings["user_api_client"]
        data = await client.check_credentials(phone, hash_password(password))
        if not data:
            return None
        user_id = data.get("user_id")
        if user_id is None:
            return None
        return (
            user_id,
            data.get("require_2fa", False),
            data.get("name", ""),
            data.get("role", ""),
            data.get("max_user_id"),
        )

    async def _create_and_send_otp(
        self, *, phone: str, user_id: int, max_user_id: int | None,
    ) -> OtpDeliveryStatus:
        otp_ttl = int(self.settings.get("otp_ttl_seconds", 300))
        code = generate_otp()
        code_hash = hash_otp(code)
        expires_at = datetime.now(UTC) + timedelta(seconds=otp_ttl)

        otp_repo = OtpRepository(self.db)
        await otp_repo.create(phone=phone, code_hash=code_hash, expires_at=expires_at)

        client: NotificationClient = self.settings["notification_client"]
        return await client.request_otp(
            user_id=user_id,
            max_user_id=max_user_id,
            code=code,
            ttl_seconds=otp_ttl,
            phone=phone,
        )

    async def _issue_access_token(self, user_id: int) -> str:
        """Создать opaque access token, сохранить хэш в БД. Вернуть сырой токен."""
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        ttl_hours = int(self.settings.get("access_token_ttl_hours", 720))
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)

        token_repo = TokenRepository(self.db)
        await token_repo.create(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=self.request.headers.get("User-Agent"),
            ip_address=self.request.remote_ip,
        )
        return token
