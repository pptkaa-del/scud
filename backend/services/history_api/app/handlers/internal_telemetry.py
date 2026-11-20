"""Internal handler для batch-записи телеметрии от locker_api.

Эндпойнт `/internal/api/v1/telemetry` закрыт снаружи через Apache
(<Location /internal> Require all denied). Доступен только сервисам внутри
Docker-сети — не требует X-Remote-User.

Используется TelemetryProducer (locker_api) после получения телеметрии
от устройства по WebSocket.
"""

from __future__ import annotations

from datetime import datetime
import json
import logging

from dacite import Config

from shared.auth import ProblemDetailsHandler

from ..history_data.db.repositories.telemetry_repository import TelemetryRepository
from .dto import TelemetryBatchRequestDTO
from .internal_hmac import InternalHmacMixin

logger = logging.getLogger(__name__)

MAX_BATCH_SIZE = 1000


class InternalTelemetryHandler(InternalHmacMixin, ProblemDetailsHandler):
    """POST /internal/api/v1/telemetry — batch-запись телеметрии системой.

    HMAC-подпись (SEC-05) проверяется ДО парсинга. Каждый reading может нести
    ``event_uid`` (ARC-02) — повторная доставка батча дедуплицируется на уровне
    строки (``ON CONFLICT (event_uid, recorded_at) DO NOTHING``).
    """

    async def post(self) -> None:
        if not self.verify_internal_hmac():
            logger.warning(
                "internal/api/v1/telemetry signature rejected from %s",
                self.request.remote_ip,
            )
            self.write_unauthorized("Invalid or expired signature")
            return

        # strict=False: internal-каналы исторически принимают тело с лишними
        # ключами (forward-compat между версиями locker_api/history_api).
        dto = self.get_json_body_as(
            TelemetryBatchRequestDTO, config=Config(), strict=False,
        )

        if not isinstance(dto.readings, list) or len(dto.readings) == 0:
            self.write_validation_error("'readings' must be a non-empty array")
            return

        if len(dto.readings) > MAX_BATCH_SIZE:
            self.write_validation_error(
                f"Too many readings (max {MAX_BATCH_SIZE} per request)",
            )
            return

        for i, reading in enumerate(dto.readings):
            if dto.locker_id and 'locker_id' not in reading:
                reading['locker_id'] = dto.locker_id
            if 'sensor_id' not in reading:
                self.write_validation_error(f"Missing 'sensor_id' in reading #{i}")
                return
            if 'sensor_type_code' not in reading:
                self.write_validation_error(
                    f"Missing 'sensor_type_code' in reading #{i}",
                )
                return
            if 'locker_id' not in reading:
                self.write_validation_error(f"Missing 'locker_id' in reading #{i}")
                return
            recorded_at = reading.get('recorded_at')
            if isinstance(recorded_at, str):
                try:
                    reading['recorded_at'] = datetime.fromisoformat(recorded_at)
                except ValueError:
                    self.write_validation_error(
                        f"Invalid 'recorded_at' format in reading #{i}",
                    )
                    return

        try:
            repo = TelemetryRepository(self.db)
            inserted = await repo.insert_batch(dto.readings)
            logger.info("Internal: inserted %s telemetry records", inserted)
            self.set_status(201)
            self.set_header("Content-Type", "application/json; charset=utf-8")
            self.write(json.dumps({"inserted": inserted}))
        except Exception as exc:
            logger.error("Internal: error inserting telemetry: %s", exc, exc_info=True)
            self.write_internal_error(str(exc))
