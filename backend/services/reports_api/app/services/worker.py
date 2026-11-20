"""Фоновый воркер генерации отчётов.

Цикл `run(stop_event)` (паттерн из notification_api/max_bot_poller):
- захватывает одну pending-задачу (`claim_pending`, FOR UPDATE SKIP LOCKED),
- выбирает генератор из registry по `report_type`,
- генерирует файл, помечает задачу ready с TTL (`expires_at`),
- при ошибке — ретраит (`requeue`) до `max_attempts`, затем `mark_failed`,
- периодически чистит истёкшие файлы (`_cleanup_expired`).

Сбои не глушатся молча: каждый логируется (warning/exception) И фиксируется в
БД как `failed`/повторная попытка — состояние наблюдаемо. PII строк отчёта не
логируется.

Останов — кооперативный через `stop_event`: при отсутствии работы воркер спит
на `asyncio.wait_for(stop_event.wait(), timeout=interval)`, так что shutdown
происходит немедленно без orphan-задач.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
import logging
from pathlib import Path

from app.reports.base import ReportGenerator
from app.reports_data.db.repositories.report_job import ReportJobRepository

logger = logging.getLogger(__name__)

# Generic, owner-safe failure message for infrastructure errors (DB/file/network).
# Сырой текст таких исключений (str(exc)) может содержать внутренние имена хостов
# и фрагменты SQL, а error_message отдаётся владельцу отчёта через report-detail.
# ValueError из validate_params — валидация пользовательского ввода, его текст
# безопасно показывать; любое другое исключение заменяем этой константой
# (полный traceback при этом логируется через logger.exception для операторов).
_GENERIC_FAILURE_MESSAGE = "Ошибка генерации отчёта"


class ReportWorker:
    """Фоновый исполнитель задач генерации отчётов."""

    def __init__(
        self,
        *,
        repo: ReportJobRepository,
        registry: dict[str, ReportGenerator],
        storage_path: Path,
        ttl_seconds: int,
        max_attempts: int,
        interval_s: float,
    ) -> None:
        self._repo = repo
        self._registry = registry
        self._storage = storage_path
        self._ttl = ttl_seconds
        self._max_attempts = max_attempts
        self._interval = interval_s
        self._storage.mkdir(parents=True, exist_ok=True)

    async def run(self, stop_event: asyncio.Event) -> None:
        """Главный цикл воркера. Завершается при установке `stop_event`."""
        logger.info("Report worker started (interval=%.1fs)", self._interval)
        while not stop_event.is_set():
            worked = False
            try:
                worked = await self._process_one()
                await self._cleanup_expired()
            except Exception:
                logger.exception("report worker iteration failed")
            if not worked:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=self._interval)
        logger.info("Report worker stopped")

    async def _process_one(self) -> bool:
        """Обработать одну задачу. Возвращает True, если задача была захвачена."""
        job = await self._repo.claim_pending()
        if job is None:
            return False

        # Реклейм зависшей running сверх бюджета попыток (COR-13): claim_pending
        # инкрементит attempts при захвате; если задача исчерпала бюджет, не
        # запускаем генерацию повторно — терминально помечаем failed, чтобы не
        # зацикливать заведомо проваленную задачу.
        if job.attempts > self._max_attempts:
            logger.warning(
                "report %s reclaimed but attempts %d exceed budget %d; failing",
                job.id,
                job.attempts,
                self._max_attempts,
            )
            await self._repo.mark_failed(
                job.id, "attempt budget exhausted (stuck running reclaimed)"
            )
            return True

        generator = self._registry.get(job.report_type)
        if generator is None:
            logger.warning("report %s: unknown report_type %r", job.id, job.report_type)
            await self._repo.mark_failed(
                job.id, f"unknown report_type: {job.report_type}"
            )
            return True

        try:
            params = generator.validate_params(job.params)
            out_path = self._storage / f"{job.id}.{generator.output_format.extension}"
            result = await generator.generate(params, out_path)
            file_name = (
                f"{job.report_type}_{job.created_at:%Y%m%d}."
                f"{generator.output_format.extension}"
            )
            expires = datetime.now(UTC) + timedelta(seconds=self._ttl)
            await self._repo.mark_ready(
                job.id,
                fmt=generator.output_format.extension,
                file_path=str(out_path),
                file_name=file_name,
                file_size=out_path.stat().st_size,
                row_count=result.row_count,
                expires_at=expires,
            )
            logger.info(
                "report %s ready (%d rows)", job.id, result.row_count
            )
        except ValueError as exc:
            # Валидация пользовательского ввода (generator.validate_params) —
            # текст безопасно показывать владельцу.
            logger.warning(
                "report %s validation failed (attempt %d): %s",
                job.id,
                job.attempts,
                exc,
            )
            if job.attempts >= self._max_attempts:
                await self._repo.mark_failed(job.id, str(exc))
            else:
                await self._repo.requeue(job.id)
        except Exception:
            # Инфраструктурный сбой (БД/файл/сеть): str(exc) может содержать
            # внутренние имена хостов / фрагменты SQL. Владельцу отдаём GENERIC,
            # полный traceback логируем для операторов (без секретов).
            logger.exception(
                "report %s generation failed (attempt %d)",
                job.id,
                job.attempts,
            )
            if job.attempts >= self._max_attempts:
                await self._repo.mark_failed(job.id, _GENERIC_FAILURE_MESSAGE)
            else:
                await self._repo.requeue(job.id)
        return True

    async def _cleanup_expired(self) -> None:
        """Удалить файлы истёкших отчётов и пометить задачи expired."""
        for job in await self._repo.list_expired_ready():
            if job.file_path:
                with contextlib.suppress(FileNotFoundError):
                    Path(job.file_path).unlink()
            await self._repo.mark_expired(job.id)
            logger.info("report %s expired, file removed", job.id)
