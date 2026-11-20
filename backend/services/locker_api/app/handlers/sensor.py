import logging

from ..locker_data.db.repositories.locker import LockerRepository
from ..locker_data.db.repositories.sensor import SensorRepository
from ..locker_data.db.repositories.sensor_type import SensorTypeRepository
from .base import BaseHandler
from .dto import PaginatedResponse

logger = logging.getLogger(__name__)


class SensorListHandler(BaseHandler):
    """Handler для работы со списком датчиков шкафа"""

    async def get(self, locker_id: str) -> None:
        """Получить список датчиков шкафа с пагинацией."""
        locker_repo = LockerRepository(self.db)
        locker = await locker_repo.get_by_id(locker_id)

        if not locker:
            self.write_not_found("Locker not found")
            return

        params = self.parse_pagination_params(default_limit=100, max_limit=1000)
        if params is None:
            return
        limit, offset = params

        sensor_repo = SensorRepository(self.db)
        sensors = await sensor_repo.get_by_locker(locker_id, limit=limit, offset=offset)
        total = await sensor_repo.count_by_locker(locker_id)

        self.write_json(
            PaginatedResponse(items=sensors, total=total, limit=limit, offset=offset)
        )

    async def post(self, locker_id: str) -> None:
        """Создать новый датчик для шкафа"""
        body = self.get_json_body()

        serial_number = body.get("serial_number")
        if not serial_number:
            self.write_validation_error("serial_number is required")
            return

        sensor_type_id = body.get("sensor_type_id")
        if not sensor_type_id:
            self.write_validation_error("sensor_type_id is required")
            return

        try:
            sensor_type_id = int(sensor_type_id)
        except (ValueError, TypeError):
            self.write_validation_error("Invalid sensor_type_id: must be an integer")
            return

        current_value = body.get("current_value")

        # Проверяем существование шкафа
        locker_repo = LockerRepository(self.db)
        locker = await locker_repo.get_by_id(locker_id)

        if not locker:
            self.write_not_found("Locker not found")
            return

        # Проверяем существование типа датчика
        sensor_type_repo = SensorTypeRepository(self.db)
        sensor_type = await sensor_type_repo.get_by_id(sensor_type_id)

        if not sensor_type:
            self.write_not_found(f"Sensor type with ID {sensor_type_id} not found")
            return

        # Создаем датчик
        sensor_repo = SensorRepository(self.db)
        sensor = await sensor_repo.create(
            locker_id=locker_id,
            serial_number=serial_number,
            sensor_type_id=sensor_type_id,
            current_value=current_value,
        )

        self.write_json(sensor, status_code=201)


class SensorDetailHandler(BaseHandler):
    """Handler для работы с конкретным датчиком"""

    async def get(self, locker_id: str, sensor_id: str) -> None:
        """Получить информацию о датчике"""
        sensor_repo = SensorRepository(self.db)
        sensor = await sensor_repo.get_by_id(sensor_id)

        if not sensor or sensor.locker_id != locker_id:
            self.write_not_found("Sensor not found")
            return

        self.write_json(sensor)

    async def patch(self, locker_id: str, sensor_id: str) -> None:
        """Обновить статус активности датчика"""
        body = self.get_json_body()

        is_active = body.get("is_active")
        if is_active is None:
            self.write_validation_error("is_active is required")
            return

        sensor_repo = SensorRepository(self.db)

        # Проверяем существование датчика
        sensor = await sensor_repo.get_by_id(sensor_id)
        if not sensor or sensor.locker_id != locker_id:
            self.write_not_found("Sensor not found")
            return

        updated_sensor = await sensor_repo.update_active_status(sensor_id, is_active)
        self.write_json(updated_sensor)

    async def delete(self, locker_id: str, sensor_id: str) -> None:
        """Удалить датчик"""
        sensor_repo = SensorRepository(self.db)

        # Проверяем существование датчика
        sensor = await sensor_repo.get_by_id(sensor_id)
        if not sensor or sensor.locker_id != locker_id:
            self.write_not_found("Sensor not found")
            return

        success = await sensor_repo.delete(sensor_id)

        if not success:
            self.write_not_found("Sensor not found")
            return

        self.set_status(204)
        self.finish()
