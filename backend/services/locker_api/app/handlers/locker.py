import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from shared.access_control import canonicalize_grant_set
from shared.search import normalize_query

from ..locker_data.db.models.locker import LockerTransport, LockerView, OperationalStatus
from ..locker_data.db.repositories.locker import LockerRepository
from ..mqtt import provisioning
from .base import BaseHandler
from .dto import BatchLockerRequestDTO, PaginatedResponse

logger = logging.getLogger(__name__)


ALLOWED_LIST_VIEWS = {LockerView.LOCATION.value}
_Q_MAX_LENGTH = 100

_ALLOWED_TRANSPORTS = {LockerTransport.MQTT.value}


@dataclass
class LockerCreateDTO:
    """Тело POST /api/v1/lockers.

    `transport` выбирает протокол устройства. Создать можно только 'mqtt'
    (релейные платы); для него обязателен `relay_id` — целочисленный
    идентификатор платы (топик com/{relay_id}, broker-user relay-{relay_id}).
    Значение 'ws' — legacy: остаётся у ранее созданных записей, но новые
    ws-шкафы не создаются (transport='ws' → ValueError → 400).
    """

    inventory_number: str
    transport: str = LockerTransport.MQTT.value
    relay_id: int | None = None
    location_address: str | None = None
    location_lat: float | None = None
    location_lon: float | None = None
    description: str | None = None

    def __post_init__(self) -> None:
        if not self.inventory_number:
            raise ValueError("inventory_number is required")
        if self.transport not in _ALLOWED_TRANSPORTS:
            raise ValueError(
                f"transport must be one of {sorted(_ALLOWED_TRANSPORTS)}"
            )
        if self.transport == LockerTransport.MQTT.value and self.relay_id is None:
            raise ValueError("relay_id is required for mqtt transport")


@dataclass
class LockerUpdateDTO:
    """Тело PUT /api/v1/locker/{uuid}.

    Семантика PATCH: отсутствующее поле (None) не изменяет соответствующую
    колонку, задано — перезаписывает. Валидация диапазона/формата намеренно
    не добавляется (dacite проверяет только типы).
    """

    inventory_number: str | None = None
    location_address: str | None = None
    location_lat: float | None = None
    location_lon: float | None = None
    description: str | None = None


class LockerListHandler(BaseHandler):
    """Handler для работы со списком шкафов"""

    async def get(self) -> None:
        """Получить список шкафов с фильтрацией"""
        connectivity = self.get_query_argument("connectivity", default=None)
        door = self.get_query_argument("door", default=None)
        operational_status = self.get_query_argument("operational_status", default=None)
        view = self.get_query_argument("view", default=None)
        q_raw = self.get_query_argument("q", default=None)

        # parse_pagination_params валидирует limit/offset, отдаёт 400 при
        # нечисловом вводе и при выходе за max_limit.
        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        if view is not None and view not in ALLOWED_LIST_VIEWS:
            self.write_validation_error(
                f"Invalid view value: {view}. Allowed: {sorted(ALLOWED_LIST_VIEWS)}"
            )
            return

        # Нормализация q: trim → пустое значение → None (не фильтруем).
        # Превышение длины — 400, чтобы не нагружать БД заведомо
        # бессмысленным LIKE-паттерном.
        try:
            q = normalize_query(q_raw, max_length=_Q_MAX_LENGTH)
        except ValueError as exc:
            self.write_validation_error(str(exc))
            return

        # Видны только шкафы с Допуском: ACM гейтит коллекцию лишь по роли
        # (в пути нет UUID), строки фильтрует locker_api. None — фильтра нет
        # (operator/administrator/full_access), [] — Допусков нет → пусто.
        # allowed идёт в ОБА запроса, иначе total разъедется с items.
        allowed = self.locker_scope().sql_filter_ids()

        repo = LockerRepository(self.db)
        lockers = await repo.get_all(
            connectivity=connectivity,
            door=door,
            operational_status=operational_status,
            limit=limit,
            offset=offset,
            view=view,
            q=q,
            allowed_locker_ids=allowed,
        )
        total = await repo.count(
            connectivity=connectivity,
            door=door,
            operational_status=operational_status,
            q=q,
            allowed_locker_ids=allowed,
        )

        self.write_json(PaginatedResponse(
            items=lockers,
            total=total,
            limit=limit,
            offset=offset,
        ))

    async def post(self) -> None:
        """Создать новый шкаф"""
        dto = self.get_json_body_as(LockerCreateDTO)

        repo = LockerRepository(self.db)

        # Проверяем уникальность инвентарного номера
        existing = await repo.get_by_inventory_number(dto.inventory_number)
        if existing:
            self.write_conflict("Locker with this inventory number already exists")
            return

        # Для mqtt-транспорта relay_id обязан быть уникальным (partial unique index).
        if dto.transport == LockerTransport.MQTT.value and dto.relay_id is not None:
            relay_existing = await repo.get_by_relay_id(dto.relay_id)
            if relay_existing:
                self.write_conflict("Locker with this relay_id already exists")
                return

        locker = await repo.create(
            inventory_number=dto.inventory_number,
            created_by=self.current_user_id,
            location_address=dto.location_address,
            location_lat=dto.location_lat,
            location_lon=dto.location_lon,
            description=dto.description,
            transport=dto.transport,
            relay_id=dto.relay_id,
        )

        # Релейному устройству выдаём broker-креды + ACL в shared config volume.
        # Брокер перечитает их по SIGHUP (~2s) и пустит relay-{id} в эфир.
        if dto.transport == LockerTransport.MQTT.value and dto.relay_id is not None:
            try:
                # provision_device — блокирующий fcntl.flock + файловый I/O;
                # уводим в thread executor, чтобы не блокировать event loop
                # (как outbound_dispatcher для multipart-сборки).
                await asyncio.to_thread(provisioning.provision_device, dto.relay_id)
            except Exception:
                # Provisioning упал ПОСЛЕ коммита locker'а → компенсируем, чтобы
                # не оставить orphaned-шкаф без broker-кредов: soft-delete только
                # что созданной записи и отдаём 5xx вместо тихого 500.
                logger.exception(
                    "Provisioning failed for relay_id=%s; compensating by deleting "
                    "locker %s", dto.relay_id, locker.id,
                )
                await repo.delete(locker.id)
                self.write_service_unavailable(
                    "Device provisioning failed; locker was not created"
                )
                return

        self.write_json(locker, status_code=201)


class LockerDetailHandler(BaseHandler):
    """Handler для работы с конкретным шкафом"""

    async def get(self, locker_id: str) -> None:
        """Получить информацию о шкафе"""
        self.parse_uuid_or_404(locker_id, what="Locker")
        repo = LockerRepository(self.db)
        locker = await repo.get_by_id(locker_id)

        if not locker:
            self.write_not_found("Locker not found")
            return

        self.write_json(locker)

    async def put(self, locker_id: str) -> None:
        """Обновить информацию о шкафе"""
        self.parse_uuid_or_404(locker_id, what="Locker")
        dto = self.get_json_body_as(LockerUpdateDTO)

        repo = LockerRepository(self.db)

        # Проверяем существование шкафа
        locker = await repo.get_by_id(locker_id)
        if not locker:
            self.write_not_found("Locker not found")
            return

        user_id = self.current_user_id
        if user_id is None:
            self.write_unauthorized("authentication required")
            return

        updated_locker = await repo.update(
            locker_id=locker_id,
            updated_by=user_id,
            inventory_number=dto.inventory_number,
            location_address=dto.location_address,
            location_lat=dto.location_lat,
            location_lon=dto.location_lon,
            description=dto.description,
        )

        self.write_json(updated_locker)

    async def delete(self, locker_id: str) -> None:
        """Удалить шкаф"""
        self.parse_uuid_or_404(locker_id, what="Locker")
        repo = LockerRepository(self.db)

        success = await repo.delete(locker_id)

        if not success:
            self.write_not_found("Locker not found")
            return

        self.set_status(204)
        self.finish()


class LockerStateHandler(BaseHandler):
    """Handler для управления состоянием шкафа"""

    async def get(self, locker_id: str) -> None:
        """Получить текущее состояние шкафа"""
        repo = LockerRepository(self.db)
        locker = await repo.get_by_id(locker_id)

        if not locker:
            self.write_not_found("Locker not found")
            return

        self.write_json({
            "locker_id": locker.id,
            "connectivity": locker.connectivity,
            "door": locker.door,
            "operational_status": locker.operational_status,
            "last_seen_at": locker.last_seen_at,
        })

    async def put(self, locker_id: str) -> None:
        """Обновить операционный статус шкафа"""
        body = self.get_json_body()

        status_str = body.get("operational_status")
        if not status_str:
            self.write_validation_error("operational_status is required")
            return

        try:
            status = OperationalStatus(status_str)
        except ValueError:
            self.write_validation_error(f"Invalid operational_status value: {status_str}")
            return

        repo = LockerRepository(self.db)
        locker = await repo.update_operational_status(
            locker_id=locker_id,
            status=status,
            updated_by=self.current_user_id,
        )

        if not locker:
            self.write_not_found("Locker not found")
            return

        self.write_json(locker)


class LockerBatchHandler(BaseHandler):
    """Handler для батч-запроса шкафов (POST /lockers/batch)"""

    async def post(self) -> None:
        """Получить информацию о нескольких шкафах за один запрос"""
        dto = self.get_json_body_as(BatchLockerRequestDTO)

        # Батч — та же область видимости, что и список: пересекаем запрошенные
        # id с Допусками, чтобы шкаф вне области нельзя было вытащить в обход
        # GET /lockers. Правило ACM здесь operator+, но defense-in-depth не
        # должен держаться на одном ACM (SEC-02).
        scope = self.locker_scope()
        locker_ids = dto.locker_ids
        if not scope.unrestricted:
            locker_ids = sorted(canonicalize_grant_set(locker_ids) & scope.locker_ids)

        repo = LockerRepository(self.db)
        result: dict[str, Any]
        if dto.view == LockerView.BRIEF.value:
            result = await repo.get_batch_brief(locker_ids)
        elif dto.view == LockerView.LOCATION.value:
            result = await repo.get_batch_location(locker_ids)
        else:
            result = await repo.get_batch(locker_ids)

        self.write_json({"items": result})
