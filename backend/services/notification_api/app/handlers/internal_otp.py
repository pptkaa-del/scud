"""Internal handler приёма OTP-кодов от auth_api: SMS-primary, MAX-fallback.

Этот эндпоинт закрыт снаружи через Apache (`<Location /internal>
Require all denied`). Между сервисами защищён HMAC-подписью
``X-Internal-Timestamp`` + ``X-Internal-Signature`` ключом
``INTERNAL_OTP_HMAC_KEY``. Подпись считается над
``timestamp + METHOD + path + body_sha256`` (SEC-20: канон привязан к
методу и пути) через ``shared.utils.compute_request_signature``.

Маршрутизация: SMS, если есть address и sms-транспорт настроен; иначе MAX.
При сбое SMS — fallback в MAX, если пользователь уже привязан (max_user_id).

Семантика ответа:
- 204: код успешно доставлен (SMS или MAX).
- 200 ``{"status": "queued_until_binding"}``: для пользователя нет ни
  одного настроенного транспорта (dev без SMS_GATEWAY_URL/MAX_BOT_TOKEN).
- 401: подпись HMAC невалидна / устарела.
- 400: невалидный DTO.
- 503: все доступные транспорты недоступны / получатель недостижим без fallback.
"""
from __future__ import annotations

import logging
from typing import TypeVar

from dacite import Config, from_dict
from dacite.exceptions import MissingValueError, WrongTypeError
from tornado.web import Finish

from shared.auth import ProblemDetailsHandler
from shared.enums import NotificationChannel, OtpDeliveryOutcome
from shared.http import UpstreamUnavailableError
from shared.metrics import record_otp_delivery
from shared.utils import body_sha256, verify_request_signature

from ..notification_data.db.repositories.outbound_max import OutboundMaxRepository
from ..services.transport import NotificationTransport, RecipientUnreachableError
from .dto import SendOtpDTO

logger = logging.getLogger(__name__)

T = TypeVar("T")


class InternalOtpSendHandler(ProblemDetailsHandler):
    """POST /internal/api/v1/otp/send — отправка OTP (SMS-primary, MAX-fallback).

    `transports` и `internal_otp_hmac_key` забираются из `Application.settings`,
    которые проставляет `main.py`. Это паттерн как у LoginHandler /
    InternalAuthCheckHandler — без override `initialize`.
    """

    def _verify_signature(self) -> bool:
        """Сравнить заголовки X-Internal-* с подписью над body_sha256.

        Если ключ не настроен (dev/CI без .env) — пропустить проверку
        (паттерн как у `InternalAuthCheckHandler`, см. internal_auth.py).
        """
        hmac_key = self.settings.get("internal_otp_hmac_key")
        if hmac_key is None:
            return True
        timestamp = self.request.headers.get("X-Internal-Timestamp", "")
        signature = self.request.headers.get("X-Internal-Signature", "")
        return verify_request_signature(
            signature=signature,
            timestamp=timestamp,
            method=self.request.method or "",
            path=self.request.path,
            body_sha256_hex=body_sha256(self.request.body or b""),
            key=hmac_key,
        )

    def _parse_dto(self) -> SendOtpDTO:
        try:
            body_dict = self.get_json_body()
            return from_dict(SendOtpDTO, body_dict, config=Config())
        except (KeyError, TypeError, ValueError, MissingValueError, WrongTypeError) as exc:
            # str(exc) у dacite содержит значение поля (телефон, код) — в лог не пишем.
            logger.warning(
                "Internal OTP send: invalid body: %s field=%s",
                type(exc).__name__, getattr(exc, "field_path", None),
            )
            self.write_validation_error(str(exc))
            raise Finish from None

    async def post(self) -> None:
        if not self._verify_signature():
            logger.warning(
                "internal/api/v1/otp/send signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        dto = self._parse_dto()
        transports: dict[NotificationChannel, NotificationTransport] = self.settings.get(
            "transports", {},
        )
        sms = transports.get(NotificationChannel.SMS)
        max_t = transports.get(NotificationChannel.MAX)

        # Политика: SMS-primary, если есть address и sms-транспорт; иначе MAX.
        primary_channel = (
            NotificationChannel.SMS if (sms is not None and dto.address)
            else NotificationChannel.MAX
        )

        outbox = OutboundMaxRepository(self.db)
        message_id = await outbox.insert(
            user_id=dto.user_id,
            max_user_id=dto.max_user_id,
            body=_format_otp_message(dto.code, dto.ttl_seconds),
            purpose=dto.purpose,
            ttl_seconds=dto.ttl_seconds,
            channel=primary_channel.value,
            address=dto.address,
        )
        text = _format_otp_message(dto.code, dto.ttl_seconds)

        # MAX-primary недоставляем прямо сейчас, если MAX не настроен ИЛИ
        # пользователь ещё не привязан (max_user_id=None): в обоих случаях код
        # паркуется в outbox, поллер довезёт его после bot_started. Не 503:
        # это штатный MAX-binding-flow, а не сбой доставки.
        if primary_channel is NotificationChannel.MAX and (
            max_t is None or dto.max_user_id is None
        ):
            logger.info("OTP queued (max not ready): user_id=%s outbox_id=%s",
                        dto.user_id, message_id)
            record_otp_delivery(NotificationChannel.MAX.value, OtpDeliveryOutcome.QUEUED)
            self.write_json({"status": "queued_until_binding"}, status_code=200)
            return

        # 1) primary
        if primary_channel is NotificationChannel.SMS:
            assert sms is not None  # гарантировано условием primary_channel выше
            ok = await self._try_send(
                sms, dto.address or "", text, outbox, message_id,
                terminal_on_permanent=(dto.max_user_id is None),
            )
            if ok:
                record_otp_delivery(NotificationChannel.SMS.value, OtpDeliveryOutcome.SENT)
                self.set_status(204)
                self.finish()
                return
            # 2) fallback → MAX (если привязан)
            if (
                max_t is not None
                and dto.max_user_id is not None
                and await self._try_send(
                    max_t, str(dto.max_user_id), text, outbox, message_id,
                    terminal_on_permanent=True,
                )
            ):
                record_otp_delivery(NotificationChannel.MAX.value, OtpDeliveryOutcome.FALLBACK_SENT)
                self.set_status(204)
                self.finish()
                return
            record_otp_delivery(NotificationChannel.SMS.value, OtpDeliveryOutcome.UNAVAILABLE)
            self.write_service_unavailable("OTP delivery unavailable")
            return

        # MAX primary (no sms configured)
        assert max_t is not None  # гарантировано ранним guard'ом выше
        if await self._try_send(
            max_t, str(dto.max_user_id), text, outbox, message_id,
            terminal_on_permanent=True,
        ):
            record_otp_delivery(NotificationChannel.MAX.value, OtpDeliveryOutcome.SENT)
            self.set_status(204)
            self.finish()
            return
        record_otp_delivery(NotificationChannel.MAX.value, OtpDeliveryOutcome.UNAVAILABLE)
        self.write_service_unavailable("OTP delivery unavailable")

    async def _try_send(
        self,
        transport: NotificationTransport,
        address: str,
        text: str,
        outbox: OutboundMaxRepository,
        message_id: int,
        *,
        terminal_on_permanent: bool,
    ) -> bool:
        """True если доставлено. На permanent — mark_failed (если terminal),
        на transient — increment_attempt; в обоих случаях False (не raise)."""
        try:
            await transport.send_text(address, text)
        except RecipientUnreachableError as exc:
            logger.warning("delivery permanent-fail outbox_id=%s: %s", message_id, exc)
            if terminal_on_permanent:
                await outbox.mark_failed(message_id, "recipient unreachable")
            return False
        except UpstreamUnavailableError as exc:
            logger.error("delivery transient-fail outbox_id=%s: %s", message_id, exc)
            await outbox.increment_attempt(message_id, str(exc))
            return False
        await outbox.mark_sent(message_id)
        return True


def _format_otp_message(code: str, ttl_seconds: int) -> str:
    """Текст сообщения с OTP-кодом.

    Не логировать! Содержит сам код. Возвращается из этой функции и сразу
    уходит в БД/Max.
    """
    minutes = max(1, ttl_seconds // 60)
    return f"Код входа: {code}\nДействует {minutes} мин."
