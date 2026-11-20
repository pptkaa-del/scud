from __future__ import annotations

from datetime import datetime
import logging

import tornado.web

from ..history_data.db.repositories.telemetry_repository import TelemetryRepository
from .base import BaseHandler
from .dto import BatchInsertResponse, PaginatedResponse, TelemetryBatchRequestDTO

logger = logging.getLogger(__name__)


class TelemetryBatchHandler(BaseHandler):
    """
    POST /api/v1/telemetry - Batch-запись телеметрии датчиков.

    Принимает массив записей телеметрии от locker_api и записывает в БД.
    Оптимизирован для batch операций (до 1000 записей за раз).
    """

    async def post(self) -> None:
        """Создать batch записей телеметрии"""
        try:
            # Получение и конвертация JSON → DTO
            dto = self.get_json_body_as(TelemetryBatchRequestDTO)

            # Валидация (dataclass типизирует readings: list, проверяем пустоту/предел)
            if len(dto.readings) == 0:
                self.write_validation_error("'readings' cannot be empty")
                return

            if len(dto.readings) > 1000:
                self.write_validation_error("Too many readings (max 1000 per request)")
                return

            # Добавление locker_id если указан в root
            if dto.locker_id:
                for reading in dto.readings:
                    if 'locker_id' not in reading:
                        reading['locker_id'] = dto.locker_id

            # Валидация обязательных полей
            for i, reading in enumerate(dto.readings):
                if 'sensor_id' not in reading:
                    self.write_validation_error(f"Missing 'sensor_id' in reading #{i}")
                    return

                if 'sensor_type_code' not in reading:
                    self.write_validation_error(f"Missing 'sensor_type_code' in reading #{i}")
                    return

                # Преобразование recorded_at если это строка
                if 'recorded_at' in reading and isinstance(reading['recorded_at'], str):
                    try:
                        reading['recorded_at'] = datetime.fromisoformat(
                            reading['recorded_at']
                        )
                    except ValueError:
                        self.write_validation_error(f"Invalid 'recorded_at' format in reading #{i}")
                        return

            # Запись в БД
            repo = TelemetryRepository(self.db)
            inserted_count = await repo.insert_batch(dto.readings)

            logger.info(f"Inserted {inserted_count} telemetry records")

            response = BatchInsertResponse(inserted=inserted_count)
            self.write_json(response, status_code=201)

        except tornado.web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error inserting telemetry: {e}", exc_info=True)
            self.write_internal_error(str(e))


class TelemetryListHandler(BaseHandler):
    """
    GET /api/v1/telemetry/list - Получение телеметрии с фильтрацией.

    Query параметры:
    - locker_id: UUID шкафа
    - sensor_id: UUID датчика
    - sensor_type_code: Код типа датчика
    - from: ISO 8601 дата начала
    - to: ISO 8601 дата конца
    - limit: Максимальное количество записей (по умолчанию 1000)
    """

    async def get(self) -> None:
        """Получить список телеметрии"""
        try:
            # Извлечение query параметров
            locker_id = self.get_query_argument("locker_id", default=None)
            sensor_id = self.get_query_argument("sensor_id", default=None)
            sensor_type_code = self.get_query_argument("sensor_type_code", default=None)
            from_str = self.get_query_argument("from", default=None)
            to_str = self.get_query_argument("to", default=None)
            limit = int(self.get_query_argument("limit", default="1000") or "1000")
            offset = int(self.get_query_argument("offset", default="0") or "0")

            # Валидация limit
            if limit < 1 or limit > 10000:
                self.write_validation_error("limit must be between 1 and 10000")
                return

            # Парсинг дат
            from_date: datetime | None = None
            to_date: datetime | None = None

            if from_str:
                try:
                    from_date = datetime.fromisoformat(from_str)
                except ValueError:
                    self.write_validation_error("Invalid 'from' date format (use ISO 8601)")
                    return

            if to_str:
                try:
                    to_date = datetime.fromisoformat(to_str)
                except ValueError:
                    self.write_validation_error("Invalid 'to' date format (use ISO 8601)")
                    return

            # Получение данных из БД
            repo = TelemetryRepository(self.db)
            telemetry = await repo.get_by_filters(
                locker_id=locker_id,
                sensor_id=sensor_id,
                sensor_type_code=sensor_type_code,
                from_date=from_date,
                to_date=to_date,
                limit=limit,
                offset=offset
            )

            # Получение общего количества (для пагинации)
            total_count = await repo.get_count(
                locker_id=locker_id,
                sensor_id=sensor_id,
                from_date=from_date,
                to_date=to_date
            )

            response = PaginatedResponse(
                items=telemetry,
                total=total_count,
                limit=limit,
                offset=offset
            )
            self.write_json(response)

        except tornado.web.HTTPError:
            raise
        except Exception as e:
            logger.error(f"Error fetching telemetry: {e}", exc_info=True)
            self.write_internal_error(str(e))
