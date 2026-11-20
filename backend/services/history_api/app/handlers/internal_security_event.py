"""Internal handler для записи событий безопасности от других backend-сервисов.

Эндпойнт `/internal/api/v1/events/security` закрыт снаружи через Apache
(<Location /internal> Require all denied). Доступен только сервисам внутри
Docker-сети — поэтому не требует X-Remote-User и не наследуется от BaseHandler,
который этот заголовок проверяет.

Используется state_monitor (locker_api) для логирования alarm-событий без
необходимости изображать какого-то конкретного пользователя.
"""

from __future__ import annotations

import json
import logging

from dacite import Config

from shared.auth import ProblemDetailsHandler

from ..history_data.db.models.security_event import EventSeverity, SecurityEvent, SecurityEventType
from ..history_data.db.repositories.media_repository import MediaRepository
from ..history_data.db.repositories.security_event_repository import SecurityEventRepository
from .dto import SecurityEventCreateDTO
from .internal_hmac import InternalHmacMixin

logger = logging.getLogger(__name__)

_DTO_CONFIG = Config(cast=[EventSeverity, SecurityEventType])

_PERSISTED_EVENT = "security_event_persisted"


def _emit_persisted_log(event: SecurityEvent) -> None:
    """SIEM-кандидат: дискретные поля записанного события → JSON через
    JsonFormatter (Фаза A). Единственная точка с присвоенным id/occurred_at.
    Без PII: только id/тип/severity/locker_id/sensor_id/occurred_at.
    """
    logger.info(
        "Security event persisted",
        extra={
            "event": _PERSISTED_EVENT,
            "id": event.id,
            "type": str(event.event_type),
            "severity": str(event.severity),
            "locker_id": event.locker_id,
            "sensor_id": event.sensor_id,
            "occurred_at": event.occurred_at.isoformat(),
        },
    )


class InternalSecurityEventHandler(InternalHmacMixin, ProblemDetailsHandler):
    """POST /internal/api/v1/events/security — запись alarm-события системой.

    HMAC-подпись (SEC-05): тело подписывается ключом ``INTERNAL_HISTORY_HMAC_KEY``,
    проверяется ДО парсинга/записи. ``event_uid`` (ARC-02): продьюсер-генерируемый
    UUID для идемпотентности; повторная доставка — no-op (``ON CONFLICT DO NOTHING``).
    И после записи, и на дубликате к событию привязываются медиа, пришедшие
    раньше него с тем же ``security_event_uid``; ошибка привязки → 500, и
    повторная доставка повторит её. SIEM-лог пишется до привязки: повторная
    доставка попадёт в ветку дубликата и его уже не напишет.
    """

    async def post(self) -> None:
        if not self.verify_internal_hmac():
            logger.warning(
                "internal/api/v1/events/security signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        # strict=False: cast-config (EventSeverity/SecurityEventType) +
        # forward-compat с лишними ключами от продьюсера.
        dto = self.get_json_body_as(
            SecurityEventCreateDTO, config=_DTO_CONFIG, strict=False,
        )

        try:
            repo = SecurityEventRepository(self.db)
            event = await repo.create(
                locker_id=dto.locker_id,
                event_type=dto.event_type,
                severity=dto.severity,
                sensor_id=dto.sensor_id,
                violated_thresholds=dto.violated_thresholds,
                metadata=dto.metadata,
                event_uid=dto.event_uid,
            )
            if event is not None:
                _emit_persisted_log(event)
            if dto.event_uid:
                await MediaRepository(self.db).link_pending_to_event(str(dto.event_uid))
            if event is None:
                # Дубликат по event_uid — событие уже записано предыдущей
                # доставкой. Идемпотентный 200 без тела, чтобы продьюсер
                # пометил доставку успешной и не ретраил бесконечно.
                logger.info(
                    "Internal: security event with event_uid=%s already exists (dedup)",
                    dto.event_uid,
                )
                self.set_status(200)
                self.set_header("Content-Type", "application/json; charset=utf-8")
                self.write(json.dumps({"status": "duplicate"}))
                return
            self.write_json(event, status_code=201)
        except Exception as exc:
            logger.error("Internal: error creating security event: %s", exc, exc_info=True)
            self.write_internal_error(str(exc))
