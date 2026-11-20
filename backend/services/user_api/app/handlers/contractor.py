import logging

from app.handlers.base import BaseHandler
from app.handlers.dto import CreateContractorDTO, PaginatedResponse, UpdateContractorDTO
from app.user_data.db.repositories.contractor import ContractorRepository
from shared.search import normalize_query

logger = logging.getLogger(__name__)

# Максимальная длина поискового запроса `q` — соответствует VARCHAR(255)
# колонки contractors.name (см. миграцию 001_init.sql).
_Q_MAX_LENGTH = 255


class ContractorListHandler(BaseHandler):
    """Обработчик для списка подрядчиков (GET /contractors, POST /contractors)"""

    async def get(self) -> None:
        """Получение списка подрядчиков с пагинацией и опциональным поиском по названию."""
        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        q_raw = self.get_query_argument("q", default=None)
        try:
            q = normalize_query(q_raw, max_length=_Q_MAX_LENGTH)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return

        repo = ContractorRepository(self.db)
        contractors = await repo.get_all(q=q, limit=limit, offset=offset)
        total = await repo.count(q=q)
        self.write_json(
            PaginatedResponse(items=contractors, total=total, limit=limit, offset=offset)
        )

    async def post(self) -> None:
        """Создание нового подрядчика"""
        if not self.current_user:
            self.write_unauthorized("User authentication required")
            return

        dto = self.get_json_body_as(CreateContractorDTO)

        created_by = self.current_user.id
        repo = ContractorRepository(self.db)
        contractor = await repo.create(name=dto.name, created_by=created_by)
        self.write_json(contractor, status_code=201)


class ContractorDetailHandler(BaseHandler):
    """Обработчик для конкретного подрядчика (GET/PUT/DELETE /contractor/{id})"""

    async def get(self, contractor_id: str) -> None:
        """Получение подрядчика по ID"""
        contractor_id_int = self.get_int_path_arg(contractor_id, "contractor_id")

        repo = ContractorRepository(self.db)
        contractor = await repo.get_by_id(contractor_id_int)

        if not contractor:
            self.write_not_found(f"Contractor with ID {contractor_id_int} not found")
            return

        self.write_json(contractor)

    async def put(self, contractor_id: str) -> None:
        """Обновление данных подрядчика"""
        contractor_id_int = self.get_int_path_arg(contractor_id, "contractor_id")

        dto = self.get_json_body_as(UpdateContractorDTO)

        repo = ContractorRepository(self.db)
        contractor = await repo.update(contractor_id_int, name=dto.name)

        if not contractor:
            self.write_not_found(f"Contractor with ID {contractor_id_int} not found")
            return

        self.write_json(contractor)

    async def delete(self, contractor_id: str) -> None:
        """Удаление подрядчика"""
        contractor_id_int = self.get_int_path_arg(contractor_id, "contractor_id")

        repo = ContractorRepository(self.db)

        # Проверяем существование
        existing = await repo.get_by_id(contractor_id_int)
        if not existing:
            self.write_not_found(f"Contractor with ID {contractor_id_int} not found")
            return

        await repo.delete(contractor_id_int)
        self.set_status(204)
