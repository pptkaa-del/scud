"""Скачивание готового файла отчёта.

GET /api/v1/report/{id}/download:
- 404 — задача не найдена;
- 409 — отчёт ещё не готов (pending/running);
- 410 — отчёт истёк/провален или файл отсутствует на диске;
- 200 — поток файла с корректными Content-Type / Content-Disposition.

Файл читается в память целиком (`read_bytes`) — для отчётов это допустимо.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.reports.formats import ReportFormat
from app.reports_data.db.models.report_job import ReportStatus
from app.reports_data.db.repositories.report_job import ReportJobRepository

from .base import BaseHandler

logger = logging.getLogger(__name__)

_GONE_TYPE = "https://scuddocs/errors/report-gone"
_GONE_TITLE = "Gone"
_GONE_DETAIL = "report file is no longer available"
_DEFAULT_FORMAT = ReportFormat.XLSX


class ReportDownloadHandler(BaseHandler):
    """Отдаёт готовый файл отчёта."""

    async def get(self, report_id: str) -> None:
        """GET /api/v1/report/{id}/download."""
        # IDOR-защита (SEC-06): owner_scope() ограничивает выборку владельцем;
        # чужой отчёт не находится → 404 (PII не утекает). Администратор — любой.
        job = await ReportJobRepository(self.db).get_by_id(
            report_id, requested_by=self.owner_scope()
        )
        if job is None:
            self.write_not_found("report not found")
            return

        if job.status in (ReportStatus.PENDING, ReportStatus.RUNNING):
            self.write_conflict("report is not ready yet")
            return

        if (
            job.status in (ReportStatus.EXPIRED, ReportStatus.FAILED)
            or not job.file_path
        ):
            self.write_problem(_GONE_TYPE, _GONE_TITLE, 410, _GONE_DETAIL)
            return

        path = Path(job.file_path)
        if not path.exists():
            self.write_problem(_GONE_TYPE, _GONE_TITLE, 410, _GONE_DETAIL)
            return

        if job.format:
            try:
                content_type = ReportFormat.from_extension(job.format).content_type
            except ValueError:
                content_type = _DEFAULT_FORMAT.content_type
        else:
            content_type = _DEFAULT_FORMAT.content_type

        file_name = job.file_name or f"{report_id}.{_DEFAULT_FORMAT.extension}"
        self.set_status(200)
        self.set_header("Content-Type", content_type)
        self.set_header(
            "Content-Disposition", f'attachment; filename="{file_name}"'
        )
        self.write(path.read_bytes())
