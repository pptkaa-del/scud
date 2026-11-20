from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from app.reports_data.db.repositories.report_job import ReportJobRepository

from .base import BaseHandler
from .dto import UpdateReportDTO
from .dto.report_dto import normalize_report_name

logger = logging.getLogger(__name__)


class ReportDetailHandler(BaseHandler):
    """Получение одной задачи по id (poll-эндпоинт) + удаление."""

    async def get(self, report_id: str) -> None:
        """GET /api/v1/report/{id}."""
        # IDOR-защита (SEC-06): owner_scope() ограничивает выборку владельцем;
        # чужой отчёт не находится → 404 (не 403, чтобы не раскрывать его
        # существование). Администратор (owner_scope() == None) читает любой.
        job = await ReportJobRepository(self.db).get_by_id(
            report_id, requested_by=self.owner_scope()
        )
        if job is None:
            self.write_not_found("report not found")
            return
        self.write_json(job)

    async def patch(self, report_id: str) -> None:
        """PATCH /api/v1/report/{id} — изменить человекочитаемое имя отчёта."""
        dto = self.get_json_body_as(UpdateReportDTO)
        if dto.name is None:
            self.write_validation_error("name is required")
            return
        try:
            name = normalize_report_name(dto.name)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return
        # IDOR-защита (SEC-06): мутация скоупится по владельцу; чужой отчёт не
        # обновляется → 404. Администратор переименовывает любой.
        job = await ReportJobRepository(self.db).update_name(
            report_id, name, requested_by=self.owner_scope()
        )
        if job is None:
            self.write_not_found("report not found")
            return
        self.write_json(job)

    async def delete(self, report_id: str) -> None:
        """DELETE /api/v1/report/{id} — soft-expire задачи + удаление файла."""
        # IDOR-защита (SEC-06): удалять можно только свой отчёт; чужой не
        # находится → 404 и mark_expired не вызывается. Администратор — любой.
        repo = ReportJobRepository(self.db)
        job = await repo.get_by_id(report_id, requested_by=self.owner_scope())
        if job is None:
            self.write_not_found("report not found")
            return
        if job.file_path:
            with contextlib.suppress(FileNotFoundError):
                Path(job.file_path).unlink()
        await repo.mark_expired(report_id)
        self.set_status(204)
