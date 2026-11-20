"""Internal step-up OTP endpoints.

`POST /internal/api/v1/step-up/issue` — выдаёт OTP-челлендж и просит
notification_api доставить его: SMS-primary на ``phone`` пользователя,
с fallback на Max-бот (см. ``notification_api`` `/internal/api/v1/otp/send`).
Max-биндинг больше не обязателен — ``users.phone`` NOT NULL, поэтому валидный
пользователь всегда достижим по SMS. Если ни один канал не доставил код
сразу (``queued_until_binding``), код ждёт привязки Max в outbox, а issue
отвечает обычным 201 с ``challenge_id``. Сбой самого notification_api — 503.

`POST /internal/api/v1/step-up/verify` — проверяет код и возвращает
исходный ``scope_payload`` (opaque dict, который выдал issue).

Оба запроса подписываются ``X-Internal-Timestamp`` + ``X-Internal-Signature``
ключом ``INTERNAL_STEP_UP_HMAC_KEY`` поверх
``timestamp + METHOD + path + sha256(body)`` — тот же generic-формат, что для
``/internal/api/v1/otp/send`` (подпись привязана к method+path, SEC-20).

Все терминальные ветки также публикуют security-event в history_api через
``SecurityEventProducer`` (severity ``info``/``warning``) и инкрементируют
Prometheus-счётчик ``scud_command_2fa_challenges_total`` по outcome. OTP-код
никогда не уходит ни в metadata, ни в логи.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hmac
import json
import logging
from typing import Any
import uuid

from dacite import Config, from_dict
from dacite.exceptions import MissingValueError, WrongTypeError

from app.auth_data.db.repositories.step_up import StepUpChallengeRepository
from app.services.notification_client import NotificationClient
from app.services.otp import generate_otp, hash_otp
from app.services.security_event_producer import SecurityEventProducer
from app.services.user_api_client import UserApiClient
from shared.http import UpstreamUnavailableError
from shared.metrics import (
    command_2fa_challenges_total,
    command_2fa_delivery_total,
)
from shared.utils import body_sha256, verify_request_signature

from .base import BaseHandler
from .dto.step_up_dto import IssueStepUpDTO, VerifyStepUpDTO
from .login import OTP_DELIVERY_UNAVAILABLE_DETAIL

logger = logging.getLogger(__name__)

# Severity для security_events (новые значения ENUM, см. shared.enums).
_SEV_INFO = "info"
_SEV_WARNING = "warning"

# event_type строки — те же, что в shared.enums.SecurityEventType, но
# дублированы здесь как литералы, чтобы import enum не тянул лишнего.
_EVT_ISSUED = "command_2fa_issued"
_EVT_VERIFIED = "command_2fa_verified"
_EVT_FAILED = "command_2fa_failed"


def _is_valid_challenge_id(value: Any) -> bool:
    """True, если ``value`` — строка, парсящаяся как UUID.

    Не-UUID challenge_id трактуется как невалидный challenge (→ 401), а не
    как 400/500: он гарантированно не соответствует ни одной записи и
    неотличим от неизвестного/просроченного.
    """
    if not isinstance(value, str) or not value:
        return False
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return False
    return True


class _SignedHandler(BaseHandler):
    """Общая HMAC-проверка для step-up endpoints."""

    def _verify_signature(self) -> bool:
        hmac_key = self.settings.get("internal_step_up_hmac_key")
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

    def _make_repo(self) -> StepUpChallengeRepository:
        return StepUpChallengeRepository(self.db)

    async def _publish_event(
        self,
        *,
        event_type: str,
        severity: str,
        locker_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        """Безопасная обёртка над SecurityEventProducer.publish.

        Продьюсер сам глушит сетевые ошибки; обёртка нужна на случай
        отсутствия продьюсера в settings (например в unit-тестах,
        которые не передают его явно).
        """
        producer: SecurityEventProducer | None = self.settings.get(
            "security_event_producer",
        )
        if producer is None:
            return
        try:
            await producer.publish(
                event_type=event_type,
                severity=severity,
                locker_id=locker_id,
                metadata=metadata,
            )
        except Exception as exc:
            logger.warning(
                "Security event %s emission failed: %s", event_type, exc,
            )


class StepUpIssueHandler(_SignedHandler):
    """POST /internal/api/v1/step-up/issue."""

    async def post(self) -> None:
        if not self._verify_signature():
            logger.warning(
                "step-up/issue signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        try:
            dto = from_dict(
                IssueStepUpDTO, self.get_json_body(), config=Config(),
            )
        except (KeyError, TypeError, ValueError,
                MissingValueError, WrongTypeError) as exc:
            self.write_validation_error(str(exc))
            return

        user_api: UserApiClient = self.settings["user_api_client"]
        try:
            ctx = await user_api.get_max_context(dto.user_id)
        except UpstreamUnavailableError:
            self.write_service_unavailable("user_api unavailable")
            return
        if ctx is None:
            self.write_validation_error("unknown user_id")
            return
        if not ctx.get("require_2fa", False):
            # Вызывающий сервис уже знает require_2fa из заголовка
            # X-Remote-Require-2FA. Если он попал сюда, значит он не сверился —
            # это его баг.
            self.write_validation_error("require_2fa is false for this user")
            return

        scope_locker_id_raw = dto.scope_payload.get("locker_id")
        scope_locker_id: str | None = (
            str(scope_locker_id_raw) if scope_locker_id_raw else None
        )
        scope_command_raw = dto.scope_payload.get("command_type")
        scope_command: str | None = (
            str(scope_command_raw) if scope_command_raw else None
        )

        max_user_id = ctx.get("max_user_id")
        phone = ctx.get("phone")
        if max_user_id is None and not phone:
            # Защитный fallback: ни телефона, ни Max-биндинга — доставить
            # некуда. Практически недостижимо (users.phone NOT NULL), но
            # сохраняем стабильный контракт для этого крайнего случая, чтобы
            # ничего ниже по цепочке не сломалось на действительно
            # недоступном пользователе.
            payload: dict[str, Any] = {
                "challenge_id": None,
                "expires_at": None,
                "delivery_status": "max_binding_required",
            }
            self.write_json(payload, status_code=201)
            command_2fa_challenges_total.labels(outcome="issued").inc()
            command_2fa_delivery_total.labels(
                status="max_binding_required",
            ).inc()
            await self._publish_event(
                event_type=_EVT_ISSUED,
                severity=_SEV_INFO,
                locker_id=scope_locker_id,
                metadata={
                    "user_id": dto.user_id,
                    "command_type": scope_command,
                    "challenge_id": None,
                    "delivery_status": "max_binding_required",
                },
            )
            return

        challenge_id = str(uuid.uuid4())
        code = generate_otp()
        ttl = dto.ttl_seconds
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)

        repo = self._make_repo()
        await repo.insert(
            challenge_id=challenge_id,
            user_id=dto.user_id,
            scope_kind=dto.scope_kind,
            scope_payload=dto.scope_payload,
            code_hash=hash_otp(code),
            expires_at=expires_at,
        )

        notification: NotificationClient = self.settings["notification_client"]
        try:
            status = await notification.request_otp(
                user_id=dto.user_id,
                max_user_id=max_user_id,
                code=code,
                ttl_seconds=ttl,
                purpose="otp_command",
                phone=phone,
            )
        except UpstreamUnavailableError:
            self.write_service_unavailable(OTP_DELIVERY_UNAVAILABLE_DETAIL)
            return

        result: dict[str, Any] = {
            "challenge_id": challenge_id,
            "expires_at": expires_at.isoformat(),
            "delivery_status": status.value,
        }
        self.write_json(result, status_code=201)
        command_2fa_challenges_total.labels(outcome="issued").inc()
        command_2fa_delivery_total.labels(status=status.value).inc()
        await self._publish_event(
            event_type=_EVT_ISSUED,
            severity=_SEV_INFO,
            locker_id=scope_locker_id,
            metadata={
                "user_id": dto.user_id,
                "command_type": scope_command,
                "challenge_id": challenge_id,
                "delivery_status": status.value,
            },
        )


class StepUpVerifyHandler(_SignedHandler):
    """POST /internal/api/v1/step-up/verify."""

    async def _reject(
        self,
        *,
        detail: str,
        reason: str,
        forbidden: bool = False,
        user_id: Any = None,
        challenge_id: str | None = None,
        locker_id: str | None = None,
    ) -> None:
        """Единый отказной путь для verify: тело + метрика + security-event.

        Все ветки verify-отказа наблюдаемо идентичны по структуре: пишут
        ProblemDetails, инкрементируют ``command_2fa_challenges_total`` с
        ``outcome="failed"`` и публикуют ``command_2fa_failed`` (severity
        ``warning``) с ``reason``. Статус-код по умолчанию 401; ветка
        «challenge не принадлежит пользователю» (``forbidden=True``) даёт 403 —
        это намеренное единственное отличие, остальные тела одинаковы по
        анти-энумерационному свойству (малформенный/не найден → один и тот же
        401, VAL-02). OTP-код и сырой challenge_id наружу/в лог не уходят.
        """
        if forbidden:
            self.write_forbidden(detail)
        else:
            self.write_unauthorized(detail)
        command_2fa_challenges_total.labels(outcome="failed").inc()
        await self._publish_event(
            event_type=_EVT_FAILED,
            severity=_SEV_WARNING,
            locker_id=locker_id,
            metadata={
                "user_id": user_id,
                "challenge_id": challenge_id,
                "reason": reason,
            },
        )

    async def post(self) -> None:
        if not self._verify_signature():
            logger.warning(
                "step-up/verify signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        body = self.get_json_body()
        # Малформенный (не-UUID) challenge_id — это просто невалидный
        # challenge: он не может соответствовать ни одной записи. Возвращаем
        # ровно тот же 401, что и для неизвестного/просроченного challenge,
        # чтобы клиент не различал «малформенный» и «не найден». Проверяем
        # форму ДО DTO-парсинга, иначе DTO-ValueError ушёл бы в 400.
        # Значение challenge_id в логи/наружу не попадает.
        if isinstance(body, dict) and not _is_valid_challenge_id(
            body.get("challenge_id"),
        ):
            await self._reject(
                detail="Challenge not found",
                reason="not_found",
                user_id=body.get("user_id"),
            )
            return

        try:
            dto = from_dict(
                VerifyStepUpDTO, body, config=Config(),
            )
        except (KeyError, TypeError, ValueError,
                MissingValueError, WrongTypeError) as exc:
            self.write_validation_error(str(exc))
            return

        repo = self._make_repo()
        row = await repo.select(dto.challenge_id)
        if row is None:
            await self._reject(
                detail="Challenge not found",
                reason="not_found",
                user_id=dto.user_id,
                challenge_id=dto.challenge_id,
            )
            return

        scope_payload_raw = row["scope_payload"]
        if isinstance(scope_payload_raw, str):
            scope_payload: dict[str, Any] = json.loads(scope_payload_raw)
        else:
            scope_payload = scope_payload_raw
        scope_locker_id_raw = scope_payload.get("locker_id")
        scope_locker_id: str | None = (
            str(scope_locker_id_raw) if scope_locker_id_raw else None
        )
        scope_command_raw = scope_payload.get("command_type")
        scope_command: str | None = (
            str(scope_command_raw) if scope_command_raw else None
        )

        if int(row["user_id"]) != dto.user_id:
            await self._reject(
                detail="Challenge does not belong to this user",
                reason="wrong_user",
                forbidden=True,
                user_id=dto.user_id,
                challenge_id=dto.challenge_id,
                locker_id=scope_locker_id,
            )
            return
        # Fast-path проверки на устаревших row["used_at"]/row["expires_at"]
        # из предыдущего select. Они НЕавторитетны (между select и записью
        # row-lock не держится) и нужны лишь чтобы дёшево отсечь очевидно
        # мёртвые challenge до сравнения кода. Авторитетные гейты —
        # reserve_attempt (бюджет) и consume (single-use) ниже: их CAS-UPDATE
        # держат инвариант в WHERE и закрывают любую гонку.
        if row["used_at"] is not None:
            await self._reject(
                detail="Challenge already used",
                reason="already_used",
                user_id=dto.user_id,
                challenge_id=dto.challenge_id,
                locker_id=scope_locker_id,
            )
            return
        if row["expires_at"] < datetime.now(UTC):
            await self._reject(
                detail="Challenge expired",
                reason="expired",
                user_id=dto.user_id,
                challenge_id=dto.challenge_id,
                locker_id=scope_locker_id,
            )
            return
        # Атомарно резервируем попытку ПЕРЕД сравнением кода (SEC-13). Один
        # CAS-UPDATE инкрементирует attempts только пока бюджет не исчерпан,
        # challenge активен и не использован; при гонке параллельных попыток
        # каждая расходует бюджет ровно один раз. None => бюджет исчерпан,
        # истёк или уже использован — отказ.
        reserved = await repo.reserve_attempt(dto.challenge_id)
        if reserved is None:
            await self._reject(
                detail="Challenge locked: too many attempts",
                reason="locked",
                user_id=dto.user_id,
                challenge_id=dto.challenge_id,
                locker_id=scope_locker_id,
            )
            return
        # Constant-time сравнение хэшей (SEC-19) — как в login-пути verify.py.
        if not hmac.compare_digest(hash_otp(dto.code), reserved["code_hash"]):
            await self._reject(
                detail="Invalid code",
                reason="invalid",
                user_id=dto.user_id,
                challenge_id=dto.challenge_id,
                locker_id=scope_locker_id,
            )
            return

        # Атомарно «сжигаем» challenge как single-use (SEC-12). consume() —
        # CAS-UPDATE: при гонке двух параллельных verify ровно один получит
        # True. Если False — challenge уже использован/истёк между нашими
        # проверками и этим UPDATE; отклоняем, чтобы привилегированная команда
        # не была подтверждена дважды.
        if not await repo.consume(dto.challenge_id):
            await self._reject(
                detail="Challenge already used",
                reason="already_used",
                user_id=dto.user_id,
                challenge_id=dto.challenge_id,
                locker_id=scope_locker_id,
            )
            return
        payload: dict[str, Any] = {
            "scope_kind": row["scope_kind"],
            "scope_payload": scope_payload,
        }
        self.write_json(payload, status_code=200)
        command_2fa_challenges_total.labels(outcome="verified").inc()
        await self._publish_event(
            event_type=_EVT_VERIFIED,
            severity=_SEV_INFO,
            locker_id=scope_locker_id,
            metadata={
                "user_id": dto.user_id,
                "command_type": scope_command,
                "challenge_id": dto.challenge_id,
            },
        )
