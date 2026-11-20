from __future__ import annotations

import logging

from app.reports.base import ReportGenerator
from app.reports_data.db.repositories.report_job import ReportJobRepository
from shared.database import AsyncPostgres
from shared.search import normalize_query

from .base import BaseHandler
from .dto import CreateReportDTO, PaginatedResponse
from .dto.report_dto import normalize_report_name

logger = logging.getLogger(__name__)

# Совпадает с VARCHAR(255) колонки name, по которой ведётся поиск.
_Q_MAX_LENGTH = 255


class ReportListHandler(BaseHandler):
    """Создание задачи на отчёт и постраничный список задач."""

    _registry: dict[str, ReportGenerator]

    def initialize(
        self, db: AsyncPostgres, registry: dict[str, ReportGenerator] | None = None
    ) -> None:
        self.db = db
        self._registry = registry if registry is not None else {}

    async def post(self) -> None:
        """POST /api/v1/reports — поставить задачу в очередь (status=pending)."""
        dto = self.get_json_body_as(CreateReportDTO)

        generator = self._registry.get(dto.report_type)
        if generator is None:
            self.write_validation_error(f"unknown report_type: {dto.report_type}")
            return

        try:
            validated = generator.validate_params(dto.params)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return

        # Хранить канонический вид из провалидированного объекта, а не сырой
        # dto.params: в БД попадают только разобранные значения, без произвольных
        # данных клиента.
        stored_params = generator.serialize_params(validated)

        try:
            name = normalize_report_name(dto.name)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return

        if self.current_user_id is None:
            self.write_unauthorized("Missing authenticated user")
            return

        repo = ReportJobRepository(self.db)
        job = await repo.create(
            report_type=dto.report_type,
            params=stored_params,
            requested_by=self.current_user_id,
            fmt=generator.output_format.extension,
            name=name,
        )

        self.write_json(
            {
                "id": job.id,
                "status": job.status,
                "report_type": job.report_type,
                "name": job.name,
                "format": job.format,
                "created_at": job.created_at,
            },
            status_code=202,
        )

    async def get(self) -> None:
        """GET /api/v1/reports — постраничный список задач."""
        pg = self.parse_pagination_params(default_limit=50, max_limit=500)
        if pg is None:
            return
        limit, offset = pg

        status = self.get_query_argument("status", None)
        report_type = self.get_query_argument("report_type", None)
        q_raw = self.get_query_argument("q", default=None)
        try:
            q = normalize_query(q_raw, max_length=_Q_MAX_LENGTH)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return

        # IDOR-защита (SEC-06): обычная роль видит только свои отчёты, фильтр
        # уходит в SQL и для страницы, и для count — чужой total не утекает.
        # Администратор (owner_scope() == None) видит все задачи.
        owner = self.owner_scope()

        repo = ReportJobRepository(self.db)
        items = await repo.list_jobs(
            status=status,
            report_type=report_type,
            q=q,
            requested_by=owner,
            limit=limit,
            offset=offset,
        )
        total = await repo.count_jobs(
            status=status, report_type=report_type, q=q, requested_by=owner
        )

        self.write_json(
            PaginatedResponse(items=items, total=total, limit=limit, offset=offset)
        )
