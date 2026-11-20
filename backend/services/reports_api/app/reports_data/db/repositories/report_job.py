from __future__ import annotations

from datetime import datetime
import json
from typing import Any

from dacite import Config, from_dict

from app.reports_data.db.models.report_job import ReportJob, ReportStatus
from shared.database import AsyncPostgres, SqlParam
from shared.search import like_filter

_ERROR_MESSAGE_MAX_LEN = 1000

# Lease-таймаут для зависших running-задач (COR-13). Если worker упал/был убит
# (OOM) после flip задачи в `running`, но до mark_ready/requeue/mark_failed,
# задача без реклейма зависает в `running` навсегда. claim_pending повторно
# захватывает running старше этого окна. Значение заведомо больше максимально
# ожидаемого времени генерации отчёта (минуты), чтобы НЕ реклеймить задачу,
# которая ещё реально выполняется (нет двойной обработки «в полёте»).
RUNNING_JOB_LEASE_SECONDS = 600

_CONFIG = Config(cast=[ReportStatus])


class ReportJobRepository:
    def __init__(self, db: AsyncPostgres) -> None:
        self._db = db

    async def create(
        self,
        *,
        report_type: str,
        params: dict[str, Any],
        requested_by: int,
        fmt: str,
        name: str | None = None,
    ) -> ReportJob:
        row = await self._db.fetch_one_strict(
            "INSERT INTO report_jobs (report_type, name, params, requested_by, format) "
            "VALUES (%s, %s, %s::jsonb, %s, %s) RETURNING *",
            (report_type, name, json.dumps(params), requested_by, fmt),
            read_only=False,
        )
        return self._to_model(row)

    async def update_name(
        self, job_id: str, name: str, *, requested_by: int | None = None
    ) -> ReportJob | None:
        """Изменить имя отчёта. Возвращает обновлённую задачу или None если не найдена.

        `requested_by` скоупит мутацию по владельцу (защита от IDOR, SEC-06):
        чужая задача не обновляется и возвращается None (handler отдаёт 404).
        ``None`` снимает фильтр — администратор может переименовать любой отчёт.
        """
        query = "UPDATE report_jobs SET name = %s WHERE id = %s"
        ps: list[SqlParam] = [name, job_id]
        if requested_by is not None:
            query += " AND requested_by = %s"
            ps.append(requested_by)
        query += " RETURNING *"
        row = await self._db.fetch_one(query, tuple(ps), read_only=False)
        return self._to_model(row) if row else None

    async def get_by_id(
        self, job_id: str, *, requested_by: int | None = None
    ) -> ReportJob | None:
        """Прочитать задачу по id.

        `requested_by` скоупит чтение по владельцу (защита от IDOR, SEC-06):
        чужая задача возвращается как None (handler отдаёт 404, не раскрывая
        существование чужого ресурса). ``None`` снимает фильтр — администратор
        читает любой отчёт.
        """
        query = "SELECT * FROM report_jobs WHERE id = %s"
        ps: list[SqlParam] = [job_id]
        if requested_by is not None:
            query += " AND requested_by = %s"
            ps.append(requested_by)
        row = await self._db.fetch_one(query, tuple(ps))
        return self._to_model(row) if row else None

    async def list_jobs(
        self,
        *,
        status: str | None,
        report_type: str | None,
        q: str | None = None,
        requested_by: int | None = None,
        limit: int,
        offset: int,
    ) -> list[ReportJob]:
        query = "SELECT * FROM report_jobs WHERE 1 = 1"
        ps: list[SqlParam] = []
        if requested_by is not None:
            query += " AND requested_by = %s"
            ps.append(requested_by)
        if status:
            query += " AND status = %s"
            ps.append(status)
        if report_type:
            query += " AND report_type = %s"
            ps.append(report_type)
        if q:
            clause, qps = like_filter(q, "name")
            query += f" AND {clause}"
            ps.extend(qps)
        query += " ORDER BY created_at DESC LIMIT %s OFFSET %s"
        ps.extend([limit, offset])
        rows = await self._db.fetch_all(query, tuple(ps))
        return [self._to_model(r) for r in rows]

    async def count_jobs(
        self,
        *,
        status: str | None,
        report_type: str | None,
        q: str | None = None,
        requested_by: int | None = None,
    ) -> int:
        query = "SELECT count(*) AS count FROM report_jobs WHERE 1 = 1"
        ps: list[SqlParam] = []
        if requested_by is not None:
            query += " AND requested_by = %s"
            ps.append(requested_by)
        if status:
            query += " AND status = %s"
            ps.append(status)
        if report_type:
            query += " AND report_type = %s"
            ps.append(report_type)
        if q:
            clause, qps = like_filter(q, "name")
            query += f" AND {clause}"
            ps.extend(qps)
        row = await self._db.fetch_one(query, tuple(ps) if ps else ())
        return int(row["count"]) if row else 0

    async def claim_pending(self) -> ReportJob | None:
        """Атомарно захватить одну pending- ИЛИ зависшую running-задачу → running.

        FOR UPDATE SKIP LOCKED делает захват безопасным при нескольких
        воркерах: каждая задача достаётся только одному. attempts += 1
        фиксирует попытку до запуска генерации (для бюджета ретраев).

        Реклейм зависших running (COR-13): если worker упал/был убит после flip
        задачи в `running`, но до mark_ready/requeue/mark_failed, задача без
        реклейма зависла бы навсегда. Один атомарный запрос захватывает и
        pending, и running старше RUNNING_JOB_LEASE_SECONDS (по started_at).
        Свежая running (в пределах lease) НЕ матчится — нет двойной обработки
        задачи «в полёте». Инкремент attempts применяется и к реклеймленным:
        исчерпавшая бюджет задача затем терминально падает в воркере.
        """
        row = await self._db.fetch_one(
            "UPDATE report_jobs "
            "SET status = 'running', started_at = now(), attempts = attempts + 1 "
            "WHERE id = ("
            "  SELECT id FROM report_jobs WHERE status = 'pending' "
            "  OR (status = 'running' AND started_at "
            f"     < now() - make_interval(secs => {RUNNING_JOB_LEASE_SECONDS})) "
            "  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1"
            ") RETURNING *",
            read_only=False,
        )
        return self._to_model(row) if row else None

    async def mark_ready(
        self,
        job_id: str,
        *,
        fmt: str,
        file_path: str,
        file_name: str,
        file_size: int,
        row_count: int,
        expires_at: datetime,
    ) -> None:
        """Пометить задачу готовой: файл сгенерирован, выставлен TTL."""
        await self._db.execute(
            "UPDATE report_jobs SET status = %s, format = %s, file_path = %s, "
            "file_name = %s, file_size = %s, row_count = %s, completed_at = now(), "
            "expires_at = %s WHERE id = %s",
            (
                ReportStatus.READY.value,
                fmt,
                file_path,
                file_name,
                file_size,
                row_count,
                expires_at,
                job_id,
            ),
        )

    async def mark_failed(self, job_id: str, error_message: str) -> None:
        """Пометить задачу проваленной (ретраи исчерпаны)."""
        await self._db.execute(
            "UPDATE report_jobs SET status = %s, error_message = %s, "
            "completed_at = now() WHERE id = %s",
            (
                ReportStatus.FAILED.value,
                error_message[:_ERROR_MESSAGE_MAX_LEN],
                job_id,
            ),
        )

    async def requeue(self, job_id: str) -> None:
        """Вернуть задачу в очередь (status=pending) для повторной попытки."""
        await self._db.execute(
            "UPDATE report_jobs SET status = %s WHERE id = %s",
            (ReportStatus.PENDING.value, job_id),
        )

    async def list_expired_ready(self) -> list[ReportJob]:
        """Готовые задачи с истёкшим TTL — кандидаты на очистку файлов."""
        rows = await self._db.fetch_all(
            "SELECT * FROM report_jobs WHERE status = 'ready' AND expires_at < now()"
        )
        return [self._to_model(r) for r in rows]

    async def mark_expired(self, job_id: str) -> None:
        """Пометить задачу истёкшей (файл удалён или удаляется)."""
        await self._db.execute(
            "UPDATE report_jobs SET status = %s WHERE id = %s",
            (ReportStatus.EXPIRED.value, job_id),
        )

    @staticmethod
    def _to_model(row: dict[str, Any]) -> ReportJob:
        data = dict(row)
        data["id"] = str(data["id"])
        if isinstance(data.get("params"), str):
            data["params"] = json.loads(data["params"])
        return from_dict(ReportJob, data, config=_CONFIG)
