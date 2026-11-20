"""
POST /api/v1/auth/verify

Шаг 2 авторизации: проверка OTP, установка сессионного cookie.

Body: {"phone": "...", "code": "..."}
Response 200: {"message": "Authentication successful"}
             + Set-Cookie: access_token=...; HttpOnly; SameSite=Strict; Path=/
"""
from datetime import UTC, datetime, timedelta
import hmac
import logging
import secrets
from typing import Any

from app.auth_data.db.repositories.otp_repository import OtpRepository
from app.auth_data.db.repositories.rate_limit_repository import RateLimitRepository
from app.auth_data.db.repositories.token_repository import TokenRepository
from app.services.otp import hash_otp
from app.services.user_api_client import UserApiClient
from shared.enums import LoginOutcome, OtpOutcome, OtpStage
from shared.http import UpstreamUnavailableError
from shared.metrics import SERVICE_NAME, login_attempts_total, otp_total
from shared.utils import hash_token

from .base import BaseHandler

logger = logging.getLogger(__name__)


class VerifyHandler(BaseHandler):
    """Проверяет OTP и выдаёт JWT access token + access cookie."""

    async def post(self) -> None:
        # Rate-limit по IP против OTP brute-force. otp_max_attempts=3 защищает
        # от перебора одного OTP, но не мешает злоумышленнику повторять
        # /verify с разными кодами при разных OTP-сессиях. Отдельный rate-limit
        # ограничивает общую частоту. `verify_rate_limit=0` отключает проверку
        # (используется в unit-тестах с замоканным db).
        rate_limit = int(self.settings.get("verify_rate_limit", 10))
        if rate_limit > 0:
            rate_window = int(self.settings.get("verify_rate_window_seconds", 60))
            ip = self.request.remote_ip
            rate_repo = RateLimitRepository(self.db)
            if not await rate_repo.check_and_record(f"verify:{ip}", rate_limit, rate_window):
                otp_total.labels(
                    service=SERVICE_NAME, stage=OtpStage.VERIFY,
                    outcome=OtpOutcome.RATE_LIMITED,
                ).inc()
                self.write_too_many_requests("Too many verify attempts, try again later")
                return

        body = self.get_json_body()
        phone = body.get("phone", "").strip()
        code = body.get("code", "").strip()

        if not phone or not code:
            self.write_validation_error("phone and code are required")
            return

        otp_repo = OtpRepository(self.db)
        otp = await otp_repo.get_active(phone)

        if not otp:
            otp_total.labels(
                service=SERVICE_NAME, stage=OtpStage.VERIFY, outcome=OtpOutcome.NO_ACTIVE_OTP,
            ).inc()
            self.write_unauthorized("Invalid or expired code")
            return

        # Атомарно резервируем попытку ПЕРЕД сравнением кода (SEC-13). Один
        # CAS-UPDATE инкрементирует attempts только пока бюджет не исчерпан,
        # OTP активен и не использован; при гонке параллельных /verify каждая
        # попытка расходует бюджет ровно один раз. None => бюджет исчерпан,
        # истёк или уже использован.
        max_attempts = int(self.settings.get("otp_max_attempts", 3))
        reserved = await otp_repo.reserve_attempt(otp.id, max_attempts)
        if reserved is None:
            otp_total.labels(
                service=SERVICE_NAME, stage=OtpStage.VERIFY,
                outcome=OtpOutcome.ATTEMPTS_EXHAUSTED,
            ).inc()
            self.write_too_many_requests("Too many invalid attempts, request a new code")
            return

        if not hmac.compare_digest(hash_otp(code), reserved["code_hash"]):
            otp_total.labels(
                service=SERVICE_NAME, stage=OtpStage.VERIFY, outcome=OtpOutcome.CODE_MISMATCH,
            ).inc()
            self.write_unauthorized("Invalid or expired code")
            return

        # OTP-код верен. Резолвим user_id ДО consume(), иначе при transient
        # 503 от user_api (rolling restart, network blip) одноразовый OTP
        # будет «сожжён» без выдачи токена — пользователь вынужден заново
        # запрашивать SMS. Single-use гарантию даёт consume() — атомарный
        # CAS-UPDATE (`used_at IS NULL`), который при гонке отдаёт успех не
        # более чем одному вызывающему, независимо от чтения get_active.
        try:
            user = await self._get_user_by_phone(phone)
        except UpstreamUnavailableError:
            self.write_service_unavailable("Authentication backend is unavailable")
            return
        if not user or not isinstance(user.get("user_id"), int):
            logger.error("Could not resolve user_id for phone after OTP success")
            self.set_status(500)
            self.finish()
            return
        user_id = user["user_id"]

        # OTP верен И user_id успешно получен — атомарно «сжигаем» его как
        # single-use. consume() — это CAS-UPDATE: при гонке двух параллельных
        # verify ровно один получит True, второй — False (SEC-12). Без этого
        # два запроса с одним валидным кодом оба выдали бы токен.
        if not await otp_repo.consume(otp.id):
            self.write_unauthorized("Invalid or expired code")
            return

        access_token = await self._issue_access_token(user_id)
        self._set_access_cookie(access_token)
        otp_total.labels(
            service=SERVICE_NAME, stage=OtpStage.VERIFY, outcome=OtpOutcome.SUCCESS,
        ).inc()
        login_attempts_total.labels(
            service=SERVICE_NAME, outcome=LoginOutcome.SUCCESS_2FA,
        ).inc()
        # Возвращаем имя и роль симметрично login без 2FA (login.py) — фронт
        # после verify должен знать, кто вошёл и куда его роутить.
        self.write_json({
            "message": "Authentication successful",
            "name": user.get("name", ""),
            "role": user.get("role", ""),
        })

    async def _issue_access_token(self, user_id: int) -> str:
        """Создать opaque access token, сохранить хэш в БД. Вернуть сырой токен."""
        token = secrets.token_urlsafe(32)
        token_hash = hash_token(token)
        ttl_hours = int(self.settings.get("access_token_ttl_hours", 720))
        expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)

        user_agent = self.request.headers.get("User-Agent")
        ip_address = self.request.remote_ip

        token_repo = TokenRepository(self.db)
        await token_repo.create(
            user_id=user_id,
            token_hash=token_hash,
            expires_at=expires_at,
            user_agent=user_agent,
            ip_address=ip_address,
        )
        return token

    async def _get_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        """Получить запись пользователя (user_id, name, role) по телефону.

        Returns:
            dict ``{user_id, name, role}`` при успехе, None если не найден.

        Raises:
            UpstreamUnavailableError если user_api недоступен.
        """
        client: UserApiClient = self.settings["user_api_client"]
        return await client.get_user_by_phone(phone)
