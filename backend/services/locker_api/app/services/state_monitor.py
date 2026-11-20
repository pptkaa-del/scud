import asyncio
import logging
from typing import Any

from shared.database import AsyncPostgres
from shared.enums import EventSeverity, SecurityEventType, UserRole

from ..locker_data.db.models.sensor_type import SensorCode
from .history_producers.security_event_producer import producer_for_sensor_type
from .notification_client import NotificationClient

logger = logging.getLogger(__name__)

# Код типа дверного датчика. Его тревога — единственная, которую подавляем
# при легально открытой двери (COR-01). Все остальные датчики
# (tamper/intrusion/temperature/power) должны срабатывать и при открытой двери.
DOOR_SENSOR_CODE = SensorCode.DOOR

# Код датчика вскрытия. Его ALARM → CRITICAL (SIEM-кандидат для немедленной реакции).
# Остальные датчики → WARNING/high.
TAMPER_SENSOR_CODE = SensorCode.TAMPER
INTRUSION_SENSOR_CODE = SensorCode.INTRUSION
_ALARM_EVENT = "security_alarm"

# sensor_code → (SecurityEventType, EventSeverity) для структурного SIEM-лога.
_ALARM_TYPE_SEVERITY: dict[str, tuple[str, str]] = {
    TAMPER_SENSOR_CODE: (SecurityEventType.DOOR_TAMPER.value, EventSeverity.CRITICAL.value),
    INTRUSION_SENSOR_CODE: (SecurityEventType.INTRUSION.value, EventSeverity.HIGH.value),
}


class StateMonitor:
    """
    Мониторит состояние датчиков и проверяет пороговые значения
    При нарушении порогов отправляет уведомления и переводит шкаф в состояние alarm
    """

    def __init__(self, db: AsyncPostgres, notification_client: NotificationClient | None = None) -> None:
        self.db = db
        # Singleton NotificationClient. None допустим для тестов: тогда
        # _send_notification создаёт разовый клиент.
        self._notification_client = notification_client
        self._running = False

    async def handle_threshold_violation(
            self,
            locker_id: str,
            sensor_id: str,
            violations: list[str],
            sensor_code: str = "",
            sensor_type_name: str = "",
            sensor_serial: str = "",
    ) -> None:
        """
        Обработать нарушение пороговых значений

        - Проверяет гарды (maintenance, открытая дверь, offline)
        - Переводит шкаф в состояние alarm при прохождении всех гардов
        - Отправляет уведомление (через интеграцию с notification_api)
        - Записывает событие в историю (через интеграцию с history_api)
        """
        from ..locker_data.db.models.locker import Connectivity, DoorState, OperationalStatus
        from ..locker_data.db.repositories.locker import LockerRepository

        locker_repo = LockerRepository(self.db)
        locker = await locker_repo.get_by_id(locker_id)
        if locker is None:
            return

        # Гард 1: обслуживание — подавить тревогу
        if locker.operational_status == OperationalStatus.MAINTENANCE:
            logger.info("Suppressing alarm for locker %s: in maintenance", locker_id)
            return

        # Гард 1b (COR-14): дедупликация повторного ALARM. Шкаф уже в ALARM из-за
        # непрерывного нарушения (например, залипший tamper) НЕ должен на каждом
        # тике sweep'а заново слать persistent-уведомление и security event —
        # иначе один застрявший датчик = ~1440 уведомлений + 1440 событий в сутки.
        # operational_status='alarm' сам служит маркером активного нарушения.
        #
        # RE-ARM (инвариант): подавление НЕ вечное. Пере-взвод происходит, когда
        # оператор сбрасывает operational_status обратно в NORMAL — следующий
        # проход увидит шкаф NORMAL и снова сработает на настоящем нарушении
        # (см. test_rearm_after_operator_reset). Восстановление значения датчика
        # ниже порога просто перестаёт триггерить handle_threshold_violation; сам
        # шкаф остаётся ALARM до сброса оператором, что и есть желаемое поведение.
        #
        # Дизайн A (намеренно грубый): пока шкаф в ALARM, новое нарушение на
        # ДРУГОМ датчике тоже подавляется. Шкаф уже помечен тревогой и ждёт
        # инспекции — повторного флуда быть не должно.
        if locker.operational_status == OperationalStatus.ALARM:
            logger.debug(
                "Suppressing re-fire for locker %s: already in ALARM (sensor %s)",
                locker_id, sensor_id,
            )
            return

        # Гард 2: дверной датчик при легально открытой двери — ожидаемо
        if sensor_code == DOOR_SENSOR_CODE and locker.door == DoorState.OPEN:
            logger.info("Suppressing door alarm for locker %s: door is open (expected)", locker_id)
            return

        # Гард 3: шкаф офлайн — некому доставить команду
        if locker.connectivity == Connectivity.OFFLINE:
            logger.info("Suppressing alarm for locker %s: device offline", locker_id)
            return

        # Переводим шкаф в состояние alarm
        await locker_repo.update_operational_status(locker_id, OperationalStatus.ALARM)

        logger.warning(
            "Locker %s entered ALARM state due to sensor %s violations: %s",
            locker_id, sensor_id, violations,
        )

        # SIEM-кандидат: дискретные поля → JSON через JsonFormatter (Фаза A).
        # tamper → CRITICAL, все остальные → WARNING. Без PII: только id/тип/severity.
        event_type, severity = _ALARM_TYPE_SEVERITY.get(
            sensor_code,
            (SecurityEventType.THRESHOLD_VIOLATION.value, EventSeverity.HIGH.value),
        )
        log_level = logging.CRITICAL if sensor_code == TAMPER_SENSOR_CODE else logging.WARNING
        logger.log(
            log_level,
            "Locker entered ALARM",
            extra={
                "event": _ALARM_EVENT,
                "type": event_type,
                "severity": severity,
                "locker_id": locker_id,
                "sensor_id": sensor_id,
            },
        )

        try:
            await self._send_notification(
                locker_id, sensor_id, violations,
                sensor_code=sensor_code,
                sensor_type_name=sensor_type_name,
                sensor_serial=sensor_serial,
            )
        except Exception as exc:
            logger.error("Failed to send threshold-violation notification: %s", exc)

        try:
            await self._log_alarm_event(
                locker_id, sensor_id, violations, sensor_type_code=sensor_code,
            )
        except Exception as exc:
            logger.error("Failed to log alarm event to history_api: %s", exc)

    async def start_monitoring(self, check_interval: int = 60) -> None:
        """
        Запустить фоновый процесс мониторинга

        Args:
            check_interval: Интервал проверки в секундах
        """
        self._running = True
        logger.info("State monitor started")

        from types import SimpleNamespace

        from ..locker_data.db.models.locker import DoorState, OperationalStatus
        from ..locker_data.db.repositories.sensor import _unpack_value
        from ..locker_data.db.repositories.threshold import ThresholdRepository

        while self._running:
            try:
                # Один JOIN-запрос возвращает (sensor, threshold)-пары для всех
                # датчиков с активными порогами — иначе на 1000 шкафов × 5
                # датчиков получаем 5K SELECT за итерацию (N+1).
                # COR-01: подавляем при открытой двери ТОЛЬКО строку самого
                # door-сенсора, а не все датчики шкафа. Широкий предикат
                # `l.door != 'open'` создавал слепое окно: пока дверь легально
                # открыта (минуты), tamper/intrusion/temperature/power полностью
                # выпадали из sweep'а. Сужение до
                # NOT (code='door' AND door='open') оставляет авторитетным
                # in-code гард (sensor_code == "door"), который и так глушит
                # ровно дверной датчик.
                # COR-14: исключаем шкафы и в MAINTENANCE, и в ALARM. Уже
                # тревожный шкаф не должен пере-вытягиваться на каждом тике —
                # иначе непрерывное нарушение заново шлёт уведомление + security
                # event каждые check_interval секунд. Дублирующий in-code гард в
                # handle_threshold_violation страхует прямые вызовы и гонки.
                rows = await self.db.fetch_all(
                    """
                    SELECT s.id AS sensor_id, s.locker_id,
                           s.current_value, s.serial_number,
                           stype.code AS sensor_code,
                           stype.name AS sensor_type_name,
                           st.id AS threshold_id,
                           st.min_value, st.max_value, st.fixed_value
                    FROM sensors s
                    INNER JOIN sensor_thresholds st ON s.id = st.sensor_id
                    INNER JOIN sensor_types stype ON s.sensor_type_id = stype.id
                    INNER JOIN lockers l ON s.locker_id = l.id
                    WHERE s.is_active = true
                      AND st.is_active = true
                      AND NOT (stype.code = %s AND l.door = %s)
                      AND l.operational_status NOT IN (%s, %s)
                    """,
                    (
                        DOOR_SENSOR_CODE,
                        DoorState.OPEN.value,
                        OperationalStatus.MAINTENANCE.value,
                        OperationalStatus.ALARM.value,
                    ),
                )

                # Группируем по sensor_id: один проход → словарь sensor_id →
                # (мета-данные сенсора, список порогов).
                sensors: dict[str, dict[str, Any]] = {}
                for row in rows:
                    sid = row["sensor_id"]
                    bucket = sensors.get(sid)
                    if bucket is None:
                        bucket = {
                            "locker_id": row["locker_id"],
                            "current_value": _unpack_value(row["current_value"]),
                            "sensor_code": row.get("sensor_code", "") or "",
                            "sensor_type_name": row.get("sensor_type_name", "") or "",
                            "sensor_serial": row.get("serial_number", "") or "",
                            "thresholds": [],
                        }
                        sensors[sid] = bucket
                    bucket["thresholds"].append(SimpleNamespace(
                        min_value=row["min_value"],
                        max_value=row["max_value"],
                        fixed_value=row["fixed_value"],
                    ))

                for sensor_id, info in sensors.items():
                    is_valid, violations = ThresholdRepository.evaluate_thresholds(
                        info["current_value"],
                        info["thresholds"],
                    )
                    if not is_valid:
                        await self.handle_threshold_violation(
                            info["locker_id"], sensor_id, violations,
                            sensor_code=info["sensor_code"],
                            sensor_type_name=info["sensor_type_name"],
                            sensor_serial=info["sensor_serial"],
                        )

                # Ждем перед следующей итерацией
                await asyncio.sleep(check_interval)

            except Exception as e:
                logger.error("Error in state monitor: %s", e, exc_info=True)
                await asyncio.sleep(check_interval)

        logger.info("State monitor stopped")

    def stop_monitoring(self) -> None:
        """Остановить фоновый процесс мониторинга"""
        self._running = False

    async def _send_notification(
        self,
        locker_id: str,
        sensor_id: str,
        violations: list[str],
        sensor_code: str = "",
        sensor_type_name: str = "",
        sensor_serial: str = "",
    ) -> None:
        """Отправить постоянное уведомление о нарушении порогового значения."""
        # Человекочитаемое описание датчика: тип, код, серийник, ID — то, чем
        # его можно отличить в UI (имя типа > код > просто ID).
        descriptor_parts: list[str] = []
        if sensor_type_name:
            descriptor_parts.append(sensor_type_name)
        attrs: list[str] = []
        if sensor_code:
            attrs.append(f"тип: {sensor_code}")
        if sensor_serial:
            attrs.append(f"серийный №{sensor_serial}")
        attrs.append(f"ID: {sensor_id}")
        descriptor = (
            f"{descriptor_parts[0]} ({', '.join(attrs)})"
            if descriptor_parts
            else ", ".join(attrs)
        )

        text = (
            f"Нарушение порогового значения в шкафу (ID: {locker_id}). "
            f"Датчик: {descriptor}. "
            f"Нарушены параметры: {', '.join(violations)}."
        )
        tags = {
            "event": SecurityEventType.THRESHOLD_VIOLATION.value,
            "locker_id": locker_id,
            "sensor_id": sensor_id,
        }
        if sensor_code:
            tags["sensor_type_code"] = sensor_code
        if sensor_type_name:
            tags["sensor_type_name"] = sensor_type_name
        if sensor_serial:
            tags["sensor_serial"] = sensor_serial

        client = self._notification_client
        if client is None:
            from .notification_client import NotificationClient
            client = NotificationClient()
        notification_id = await client.create_persistent(
            text=text,
            tags=tags,
            roles=[UserRole.OPERATOR.value, UserRole.ADMINISTRATOR.value],
        )
        if notification_id:
            logger.info(
                "Created threshold-violation notification %s for locker %s",
                notification_id, locker_id,
            )
        else:
            logger.warning(
                "Failed to create threshold-violation notification for locker %s", locker_id,
            )

    async def _log_alarm_event(
        self,
        locker_id: str,
        sensor_id: str,
        violations: list[str],
        sensor_type_code: str | None = None,
    ) -> None:
        """Записать событие тревоги в history_api через outbound queue.

        Маршрутизация по `sensor_type_code`:
          - 'intrusion' → IntrusionProducer (event_type=intrusion, severity=high)
          - 'tamper'    → DoorTamperProducer (event_type=door_tamper, severity=critical)
          - прочее      → ThresholdViolationProducer (severity=high)

        Само событие складывается в outbound queue и доставляется
        `OutboundDispatcher`'ом — fire-and-forget на этом уровне корректен,
        потери не происходит даже при недоступности history_api.
        """
        producer = producer_for_sensor_type(self.db, sensor_type_code)
        await producer.publish(
            locker_id=locker_id,
            sensor_id=sensor_id,
            violations=violations,
            sensor_type_code=sensor_type_code or None,
        )
        logger.info(
            "Enqueued security event for locker %s, sensor %s (event_type=%s)",
            locker_id, sensor_id, producer.EVENT_TYPE,
        )
