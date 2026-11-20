"""HTTP-клиент auth_api → notification_api.

Используется для доставки OTP-кодов в Max-бот через
`POST /internal/api/v1/otp/send`. Запросы подписываются HMAC-SHA256
ключом ``INTERNAL_OTP_HMAC_KEY`` поверх ``timestamp + METHOD + path +
sha256(body)`` (SEC-20: канон привязан к методу и пути) — тот же
generic-формат, что для любых будущих inter-service подписанных
endpoint'ов (см. ``shared.utils.compute_request_signature``).
"""

from __future__ import annotations

from enum import Enum
import json
import logging
import time

from tornado.httpclient import HTTPError

from shared.http import RetryHTTPClient, UpstreamUnavailableError
from shared.utils import body_sha256, compute_request_signature

logger = logging.getLogger(__name__)


_UPSTREAM = "notification_api"

# Подписываемые (method, path) — должны совпасть с self.request.method /
# self.request.path на стороне InternalOtpSendHandler.
_OTP_SEND_METHOD = "POST"
_OTP_SEND_PATH = "/internal/api/v1/otp/send"


class OtpDeliveryStatus(str, Enum):
    """Результат вызова `/internal/api/v1/otp/send`.

    `SENT` — сообщение ушло в Max (HTTP 204).
    `QUEUED_UNTIL_BINDING` — пользователь ещё не привязал Max-бот,
    notification_api положил OTP в outbox и доставит после bot_started.
    """

    SENT = "sent"
    QUEUED_UNTIL_BINDING = "queued_until_binding"


class NotificationClient:
    """Async клиент notification_api."""

    def __init__(
        self,
        base_url: str,
        http: RetryHTTPClient | None = None,
        hmac_key: bytes | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()
        # Если ключ не задан — dev/CI без секретов. notification_api в
        # таком же режиме пропустит запрос без подписи. В production
        # ключ ОБЯЗАН быть установлен в обоих сервисах.
        self._hmac_key = hmac_key

    async def request_otp(
        self,
        user_id: int,
        max_user_id: int | None,
        code: str,
        ttl_seconds: int,
        purpose: str = "otp_login",
        *,
        phone: str | None = None,
    ) -> OtpDeliveryStatus:
        """POST /internal/api/v1/otp/send.

        `purpose` идёт в outbox-запись на стороне notification_api для
        аудита/фильтрации (otp_login | otp_command | ...). Совместимо с
        существующими вызовами: default ``otp_login``.

        Returns:
            OtpDeliveryStatus.SENT — 204.
            OtpDeliveryStatus.QUEUED_UNTIL_BINDING — 200 {"status": "queued_until_binding"}.

        Raises:
            UpstreamUnavailableError(upstream=notification_api):
                503 от notification_api, 5xx, сетевой сбой, или
                непредвиденный 4xx.
        """
        url = f"{self._base}{_OTP_SEND_PATH}"
        body = json.dumps({
            "user_id": user_id,
            "max_user_id": max_user_id,
            "code": code,
            "ttl_seconds": ttl_seconds,
            "purpose": purpose,
            "address": phone,
        }).encode("utf-8")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._hmac_key is not None:
            timestamp = str(int(time.time()))
            signature = compute_request_signature(
                timestamp=timestamp,
                method=_OTP_SEND_METHOD,
                path=_OTP_SEND_PATH,
                body_sha256_hex=body_sha256(body),
                key=self._hmac_key,
            )
            headers["X-Internal-Timestamp"] = timestamp
            headers["X-Internal-Signature"] = signature

        try:
            resp = await self._http.fetch(
                url,
                method=_OTP_SEND_METHOD,
                body=body,
                headers=headers,
            )
        except HTTPError as exc:
            logger.error(
                "notification_api /internal/api/v1/otp/send returned %s: %s",
                exc.code, exc,
            )
            raise UpstreamUnavailableError(
                f"notification_api returned {exc.code}",
                upstream=_UPSTREAM,
                status=exc.code,
            ) from exc
        except Exception as exc:
            logger.error("notification_api request_otp failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc

        if resp.code == 204:
            return OtpDeliveryStatus.SENT

        if resp.code == 200:
            try:
                payload = json.loads(resp.body or b"{}")
            except (ValueError, TypeError) as exc:
                raise UpstreamUnavailableError(
                    f"notification_api returned non-JSON 200: {exc}",
                    upstream=_UPSTREAM,
                ) from exc
            status = (payload or {}).get("status")
            if status == OtpDeliveryStatus.QUEUED_UNTIL_BINDING.value:
                return OtpDeliveryStatus.QUEUED_UNTIL_BINDING
            raise UpstreamUnavailableError(
                f"notification_api 200 with unexpected status: {status!r}",
                upstream=_UPSTREAM,
            )

        # Любые иные коды (302, 100, ...) — нештатно. Считаем сервис недоступным.
        raise UpstreamUnavailableError(
            f"notification_api returned unexpected {resp.code}",
            upstream=_UPSTREAM,
            status=resp.code,
        )
