from __future__ import annotations

from datetime import UTC, datetime
import json
from typing import Any

from dacite import from_dict

from shared.database import AsyncPostgres, SqlParam

from ..models.sensor_telemetry import SensorTelemetry


class TelemetryRepository:
    """
    Репозиторий для работы с телеметрией датчиков.

    Обеспечивает batch-вставку данных для оптимизации производительности
    при больших объемах данных (~167 записей/сек).
    """

    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def insert_batch(
        self,
        telemetry_records: list[dict[str, Any]]
    ) -> int:
        """
        Batch-вставка записей телеметрии.

        Args:
            telemetry_records: Список словарей с данными телеметрии

        Returns:
            Количество вставленных записей

        Example:
            records = [
                {
                    "locker_id": "uuid",
                    "sensor_id": "uuid",
                    "sensor_type_code": "temperature",
                    "value": 25.5,
                    "recorded_at": datetime.now()
                }
            ]
            count = await repo.insert_batch(records)
        """
        if not telemetry_records:
            return 0

        # Подготовка значений для batch insert
        values = []
        for record in telemetry_records:
            metadata = record.get('metadata')
            values.append((
                record['locker_id'],
                record['sensor_id'],
                record['sensor_type_code'],
                record.get('value'),
                record.get('value_text'),
                record.get('recorded_at', datetime.now()),
                json.dumps(metadata) if metadata else '{}',
                # ARC-02: per-reading event_uid для идемпотентности повторной
                # доставки батча. NULL допустим (обратная совместимость).
                record.get('event_uid'),
            ))

        # Batch INSERT одним запросом: INSERT INTO ... VALUES (%s,...), (%s,...), ...
        # ON CONFLICT (event_uid, recorded_at): партиционная таблица требует
        # включить ключ партиционирования (recorded_at) в UNIQUE-индекс
        # (ограничение Postgres). Повторная доставка батча — no-op.
        placeholders = ", ".join(["(%s, %s, %s, %s, %s, %s, %s, %s)"] * len(values))
        insert_query = f"""
            INSERT INTO sensor_telemetry (
                locker_id, sensor_id, sensor_type_code,
                value, value_text, recorded_at, metadata, event_uid
            ) VALUES {placeholders}
            ON CONFLICT (event_uid, recorded_at) DO NOTHING
            RETURNING 1
        """
        flat_params = [param for row in values for param in row]
        # ARC-02: возвращаем РЕАЛЬНОЕ число вставленных строк. При ON CONFLICT
        # DO NOTHING продьюсер-дубликаты не вставляются, поэтому len(values)
        # завышал бы счётчик. RETURNING отдаёт по строке на каждую фактически
        # вставленную запись — их и считаем (надёжнее cur.rowcount через
        # write-side fetch_all).
        rows = await self._db.fetch_all(insert_query, flat_params, read_only=False)
        return len(rows)

    async def get_by_filters(
        self,
        locker_id: str | None = None,
        sensor_id: str | None = None,
        sensor_type_code: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None,
        limit: int = 1000,
        offset: int = 0
    ) -> list[SensorTelemetry]:
        """
        Получить телеметрию с фильтрацией.

        Args:
            locker_id: UUID шкафа
            sensor_id: UUID датчика
            sensor_type_code: Код типа датчика
            from_date: Начало временного диапазона
            to_date: Конец временного диапазона
            limit: Максимальное количество записей

        Returns:
            Список записей телеметрии
        """
        query = "SELECT * FROM sensor_telemetry WHERE 1=1"
        params: list[SqlParam] = []

        if locker_id:
            query += " AND locker_id = %s"
            params.append(locker_id)

        if sensor_id:
            query += " AND sensor_id = %s"
            params.append(sensor_id)

        if sensor_type_code:
            query += " AND sensor_type_code = %s"
            params.append(sensor_type_code)

        if from_date:
            query += " AND recorded_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND recorded_at <= %s"
            params.append(to_date)

        query += " ORDER BY recorded_at DESC LIMIT %s OFFSET %s"
        params.append(limit)
        params.append(offset)

        rows = await self._db.fetch_all(query, tuple(params))
        return [from_dict(SensorTelemetry, row) for row in rows]

    DEFAULT_COUNT_WINDOW_DAYS = 30

    async def get_count(
        self,
        locker_id: str | None = None,
        sensor_id: str | None = None,
        from_date: datetime | None = None,
        to_date: datetime | None = None
    ) -> int:
        """Получить количество записей телеметрии с фильтрацией.

        Если ни ``from_date``, ни ``to_date`` не заданы, неявно ограничивается
        последними DEFAULT_COUNT_WINDOW_DAYS днями. Без этого ограничения
        ``COUNT(*)`` принудительно сканирует все ~36+ месячных партиций
        (десятки миллионов строк) — никакая партиционная подрезка не работает.
        """
        query = "SELECT COUNT(*) as count FROM sensor_telemetry WHERE 1=1"
        params: list[SqlParam] = []

        if locker_id:
            query += " AND locker_id = %s"
            params.append(locker_id)

        if sensor_id:
            query += " AND sensor_id = %s"
            params.append(sensor_id)

        # Принуждаем хоть какой-то верхний/нижний bound, чтобы partition pruning
        # отсекал старые партиции. Если caller совсем без диапазона —
        # подразумеваем последние DEFAULT_COUNT_WINDOW_DAYS дней.
        if from_date is None and to_date is None:
            from datetime import timedelta
            from_date = datetime.now(UTC) - timedelta(days=self.DEFAULT_COUNT_WINDOW_DAYS)

        if from_date:
            query += " AND recorded_at >= %s"
            params.append(from_date)

        if to_date:
            query += " AND recorded_at <= %s"
            params.append(to_date)

        row = await self._db.fetch_one(query, tuple(params) if params else ())
        return row['count'] if row else 0
