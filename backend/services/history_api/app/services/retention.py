from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
import logging
import re

from shared.database import AsyncPostgres
from shared.metrics import inc_partition_create_failure

logger = logging.getLogger(__name__)

_PARTITION_RE = re.compile(r"^sensor_telemetry_(\d{4})_(\d{2})$")

# Имя единственной партиционированной родительской таблицы history_api.
# Используется и в SQL-запросах create_monthly_partition, и как label метрики
# scud_partition_create_failures_total — изменить здесь, чтобы обновить оба места.
_PARTITIONED_TABLE = "sensor_telemetry"

# DEFAULT-партиция sensor_telemetry — safety net для out-of-range строк.
# Целиком не удаляется (_drop_old_partitions работает только с YYYY_MM),
# поэтому устаревшие строки чистятся отдельным DELETE по recorded_at (COR-09).
_DEFAULT_PARTITION = "sensor_telemetry_default"


def _assert_safe_partition_name(name: str) -> None:
    """Гарантирует, что имя таблицы — валидная партиция sensor_telemetry_YYYY_MM.

    Защита от SQL/DDL injection: единственный путь, по которому имя таблицы
    попадает в DROP TABLE — это f-string, поэтому валидация имени критична.
    """
    if not _PARTITION_RE.match(name):
        raise ValueError(f"Unsafe partition name: {name!r}")


class RetentionService:
    """
    Фоновый сервис очистки устаревших данных и управления партициями.

    Периодически удаляет старые записи, управляет партициями таблиц
    и очищает файлы фотографий, вышедшие за пределы срока хранения.
    """

    def __init__(self, db: AsyncPostgres, retention_months: int = 24) -> None:
        self._db = db
        self._retention_months = retention_months
        self._running = False

    async def start(self, interval: int = 3600) -> None:
        """Запустить фоновый процесс очистки"""
        self._running = True
        logger.info(
            "Retention service started (interval=%ds, retention=%d months)",
            interval,
            self._retention_months,
        )

        while self._running:
            try:
                await self._drop_old_partitions()
                await self._delete_old_rows()
                await self._create_future_partitions()
                await asyncio.sleep(interval)
            except Exception as e:
                logger.error("Error in retention service: %s", e, exc_info=True)
                await asyncio.sleep(interval)

        logger.info("Retention service stopped")

    def stop(self) -> None:
        """Остановить фоновый процесс очистки"""
        self._running = False

    # ------------------------------------------------------------------
    # Вычисление даты отсечки
    # ------------------------------------------------------------------

    def _get_cutoff_date(self) -> datetime:
        """Вернуть дату, ранее которой данные считаются устаревшими."""
        now = datetime.now(UTC)
        year = now.year
        month = now.month - self._retention_months
        while month <= 0:
            month += 12
            year -= 1
        return datetime(year, month, 1, tzinfo=UTC)

    # ------------------------------------------------------------------
    # Управление партициями
    # ------------------------------------------------------------------

    async def _drop_old_partitions(self) -> None:
        """Удалить партиции старше срока хранения."""
        cutoff = self._get_cutoff_date()

        rows = await self._db.fetch_all(
            """
            SELECT table_name FROM information_schema.tables
            WHERE table_name LIKE 'sensor_telemetry_20%%'
              AND table_schema = 'public'
            ORDER BY table_name
            """,
            (),
        )

        dropped = 0
        for row in rows:
            table_name = row["table_name"]
            match = _PARTITION_RE.match(table_name)
            if not match:
                continue

            part_year, part_month = int(match.group(1)), int(match.group(2))
            partition_date = datetime(part_year, part_month, 1, tzinfo=UTC)

            if partition_date < cutoff:
                _assert_safe_partition_name(table_name)
                await self._db.execute(
                    f"DROP TABLE IF EXISTS {table_name}",
                    (),
                )
                dropped += 1
                logger.info("Dropped old partition: %s", table_name)

        logger.info("Dropped %d old partition(s)", dropped)

    async def _delete_old_rows(self) -> None:
        """Удалить устаревшие строки из таблиц без партиционирования."""
        cutoff = self._get_cutoff_date()

        # security_events
        await self._db.execute(
            "DELETE FROM security_events WHERE occurred_at < %s",
            [cutoff],
        )
        logger.info("Deleted old security_events before %s", cutoff.isoformat())

        # command_history
        await self._db.execute(
            "DELETE FROM command_history WHERE created_at < %s",
            [cutoff],
        )
        logger.info("Deleted old command_history before %s", cutoff.isoformat())

        # locker_media — hard-delete устаревших записей (байты хранятся в БД)
        await self._db.execute(
            "DELETE FROM locker_media WHERE captured_at < %s",
            [cutoff],
        )
        logger.info("Deleted old locker_media before %s", cutoff.isoformat())

        # telemetry_aggregations
        await self._db.execute(
            "DELETE FROM telemetry_aggregations WHERE period_start < %s",
            [cutoff],
        )
        logger.info("Deleted old telemetry_aggregations before %s", cutoff.isoformat())

        # sensor_telemetry_default (COR-09) — DEFAULT-партиция не удаляется целиком
        # (_drop_old_partitions трогает только YYYY_MM), поэтому устаревшие
        # out-of-range строки чистим явным DELETE по тому же горизонту хранения.
        await self._db.execute(
            f"DELETE FROM {_DEFAULT_PARTITION} WHERE recorded_at < %s",
            [cutoff],
        )
        logger.info(
            "Deleted old %s rows before %s", _DEFAULT_PARTITION, cutoff.isoformat()
        )

    LOOKAHEAD_MONTHS = 6

    async def _create_future_partitions(self) -> None:
        """Гарантировать партиции на текущий месяц и LOOKAHEAD_MONTHS вперёд.

        Идемпотентно (create_monthly_partition использует CREATE TABLE IF NOT
        EXISTS), поэтому безопасно вызывать на каждой итерации цикла. Текущий
        месяц включён в диапазон: если сервис впервые стартует посреди
        неподдерживаемого периода, текущая запись не уйдёт в DEFAULT-партицию.
        """
        now = datetime.now(UTC)

        # COR-06: создание каждой партиции изолировано собственным try/except.
        # Сбой одного месяца (transient DB error, lock и т.п.) НЕ должен
        # обрывать цикл и оставлять последующие месяцы без партиций. Логируем
        # и продолжаем; в конце сообщаем агрегат успехов/сбоев.
        succeeded = 0
        failed = 0
        for i in range(self.LOOKAHEAD_MONTHS + 1):
            month = now.month + i
            year = now.year
            while month > 12:
                month -= 12
                year += 1

            start_date = date(year, month, 1)
            try:
                await self._db.execute(
                    "SELECT create_monthly_partition(%s, %s)",
                    [_PARTITIONED_TABLE, start_date],
                )
                succeeded += 1
                logger.info("Ensured partition for %s", start_date.isoformat())
            except Exception as e:
                failed += 1
                logger.error(
                    "Failed to ensure partition for %s: %s",
                    start_date.isoformat(),
                    e,
                    exc_info=True,
                )
                inc_partition_create_failure(_PARTITIONED_TABLE)

        logger.info(
            "Partition provisioning done: %d ensured, %d failed", succeeded, failed
        )

