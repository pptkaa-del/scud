"""Unit-тесты для StateMonitor"""
import asyncio
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.locker_data.db.models.locker import Connectivity, DoorState, OperationalStatus
from app.services.state_monitor import StateMonitor


def _make_mock_locker(
    connectivity=Connectivity.ONLINE,
    door=DoorState.CLOSED,
    operational_status=OperationalStatus.NORMAL,
):
    """Вспомогательная функция: создать мок-объект шкафа с нужными полями."""
    locker = MagicMock()
    locker.connectivity = connectivity
    locker.door = door
    locker.operational_status = operational_status
    return locker


class TestStateMonitor(unittest.TestCase):
    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()

        self.state_monitor = StateMonitor(self.db_mock)

    async def _test_handle_threshold_violation_async(self):
        """Асинхронный тест: обработка нарушения пороговых значений"""
        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        violations = ["Value 55.0 exceeds max threshold 50.0"]

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker())
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                locker_id, sensor_id, violations, sensor_code="temperature",
            )

            # Проверяем, что шкаф переведен в состояние ALARM
            repo_instance.update_operational_status.assert_called_once_with(
                locker_id, OperationalStatus.ALARM,
            )

    def test_handle_threshold_violation(self):
        """Тест: обработка нарушения пороговых значений"""
        asyncio.run(self._test_handle_threshold_violation_async())

    async def _test_monitoring_cycle_async(self):
        """Цикл мониторинга: один JOIN-row per (sensor, threshold), in-memory check."""
        # После refactor StateMonitor запрос возвращает (sensor, threshold)-пары
        # с полями min/max/fixed_value прямо в строке. evaluate_thresholds
        # вызывается без обращения к БД.
        sensor_row = {
            "sensor_id": "660e8400-e29b-41d4-a716-446655440001",
            "locker_id": "550e8400-e29b-41d4-a716-446655440000",
            "current_value": 55.0,
            "sensor_code": "temperature",
            "sensor_type_name": "Температура",
            "serial_number": "SN-TEMP-001",
            "threshold_id": 1,
            "min_value": None,
            "max_value": 50.0,
            "fixed_value": None,
        }

        self.db_mock.fetch_all.return_value = [sensor_row]

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as locker_repo_mock:

            locker_instance = locker_repo_mock.return_value
            locker_instance.get_by_id = AsyncMock(return_value=_make_mock_locker())
            locker_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            # Запускаем мониторинг на одну итерацию
            task = asyncio.create_task(
                self.state_monitor.start_monitoring(check_interval=0.1)
            )

            # Даем время на одну итерацию
            await asyncio.sleep(0.2)

            # Останавливаем мониторинг
            self.state_monitor.stop_monitoring()
            await task

            # Проверяем, что состояние шкафа было обновлено
            locker_instance.update_operational_status.assert_called_with(
                "550e8400-e29b-41d4-a716-446655440000",
                OperationalStatus.ALARM,
            )

    def test_monitoring_cycle(self):
        """Тест: цикл мониторинга состояния датчиков"""
        asyncio.run(self._test_monitoring_cycle_async())

    async def _test_monitoring_no_violations_async(self):
        """Мониторинг без нарушений: значение 25.0 при max=50.0 не триггерит alarm."""
        sensor_row = {
            "sensor_id": "660e8400-e29b-41d4-a716-446655440001",
            "locker_id": "550e8400-e29b-41d4-a716-446655440000",
            "current_value": 25.0,
            "sensor_code": "temperature",
            "sensor_type_name": "Температура",
            "serial_number": "SN-TEMP-001",
            "threshold_id": 1,
            "min_value": None,
            "max_value": 50.0,
            "fixed_value": None,
        }

        self.db_mock.fetch_all.return_value = [sensor_row]

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as locker_repo_mock:

            locker_instance = locker_repo_mock.return_value
            locker_instance.get_by_id = AsyncMock(return_value=_make_mock_locker())
            locker_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            # Запускаем мониторинг на одну итерацию
            task = asyncio.create_task(
                self.state_monitor.start_monitoring(check_interval=0.1)
            )

            # Даем время на одну итерацию
            await asyncio.sleep(0.2)

            # Останавливаем мониторинг
            self.state_monitor.stop_monitoring()
            await task

            # Проверяем, что состояние шкафа НЕ было обновлено
            locker_instance.update_operational_status.assert_not_called()

    def test_monitoring_no_violations(self):
        """Тест: мониторинг без нарушений пороговых значений"""
        asyncio.run(self._test_monitoring_no_violations_async())


class TestStateMonitorNotificationIntegration(unittest.TestCase):
    """Тесты интеграции StateMonitor с NotificationClient."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        self.state_monitor = StateMonitor(self.db_mock)

    async def _test_sends_notification_on_violation_async(self):
        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        violations = ["Value 55.0 exceeds max threshold 50.0"]

        create_persistent_mock = AsyncMock(return_value="new-notif-id")

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock, \
             patch(
                 'app.services.notification_client.NotificationClient.create_persistent',
                 create_persistent_mock,
             ):
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker())
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._log_alarm_event = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                locker_id, sensor_id, violations, sensor_code="temperature",
            )

        create_persistent_mock.assert_called_once()
        call_kwargs = create_persistent_mock.call_args[1]
        self.assertEqual(call_kwargs["tags"]["event"], "threshold_violation")
        self.assertEqual(call_kwargs["tags"]["locker_id"], locker_id)
        self.assertEqual(call_kwargs["tags"]["sensor_id"], sensor_id)
        self.assertIn("operator", call_kwargs.get("roles", []))

    def test_handle_threshold_violation_sends_notification(self):
        asyncio.run(self._test_sends_notification_on_violation_async())

    async def _test_notification_includes_sensor_identity_async(self):
        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        violations = ["Value 55.0 exceeds max threshold 50.0"]

        create_persistent_mock = AsyncMock(return_value="new-notif-id")

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock, \
             patch(
                 'app.services.notification_client.NotificationClient.create_persistent',
                 create_persistent_mock,
             ):
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker())
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._log_alarm_event = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                locker_id, sensor_id, violations,
                sensor_code="temperature",
                sensor_type_name="Температура",
                sensor_serial="SN-TEMP-001",
            )

        call_kwargs = create_persistent_mock.call_args[1]
        text = call_kwargs["text"]
        tags = call_kwargs["tags"]

        # Текст содержит человекочитаемое имя типа, код и серийник —
        # оператор может опознать датчик не разворачивая UUID.
        self.assertIn("Температура", text)
        self.assertIn("temperature", text)
        self.assertIn("SN-TEMP-001", text)
        self.assertIn(sensor_id, text)

        # Теги тоже несут идентификацию для машинной фильтрации.
        self.assertEqual(tags["sensor_type_code"], "temperature")
        self.assertEqual(tags["sensor_type_name"], "Температура")
        self.assertEqual(tags["sensor_serial"], "SN-TEMP-001")
        self.assertEqual(tags["sensor_id"], sensor_id)
        self.assertEqual(tags["locker_id"], locker_id)
        self.assertEqual(tags["event"], "threshold_violation")

    def test_notification_includes_sensor_identity(self):
        asyncio.run(self._test_notification_includes_sensor_identity_async())

    async def _test_notification_failure_doesnt_break_alarm_async(self):
        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        violations = ["Value 55.0 exceeds max threshold 50.0"]

        failing_create = AsyncMock(side_effect=Exception("notification_api down"))

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock, \
             patch(
                 'app.services.notification_client.NotificationClient.create_persistent',
                 failing_create,
             ):
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker())
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._log_alarm_event = AsyncMock()

            # Should not raise even if notification fails
            await self.state_monitor.handle_threshold_violation(
                locker_id, sensor_id, violations, sensor_code="temperature",
            )

            # ALARM state transition still happens
            repo_instance.update_operational_status.assert_called_once()

    def test_handle_threshold_violation_notification_failure_doesnt_break_alarm(self):
        asyncio.run(self._test_notification_failure_doesnt_break_alarm_async())

    async def _test_monitoring_cycle_sends_notification_async(self):
        sensor_row = {
            "sensor_id": "660e8400-e29b-41d4-a716-446655440001",
            "locker_id": "550e8400-e29b-41d4-a716-446655440000",
            "current_value": 55.0,
            "sensor_code": "temperature",
            "sensor_type_name": "Температура",
            "serial_number": "SN-TEMP-001",
            "threshold_id": 1,
            "min_value": None,
            "max_value": 50.0,
            "fixed_value": None,
        }
        self.db_mock.fetch_all.return_value = [sensor_row]

        create_persistent_mock = AsyncMock(return_value="cycle-notif-id")

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as locker_mock, \
             patch(
                 'app.services.notification_client.NotificationClient.create_persistent',
                 create_persistent_mock,
             ):
            locker_instance = locker_mock.return_value
            locker_instance.get_by_id = AsyncMock(return_value=_make_mock_locker())
            locker_instance.update_operational_status = AsyncMock()

            self.state_monitor._log_alarm_event = AsyncMock()

            task = asyncio.create_task(self.state_monitor.start_monitoring(check_interval=0.1))
            await asyncio.sleep(0.2)
            self.state_monitor.stop_monitoring()
            await task

        create_persistent_mock.assert_called()

    def test_monitoring_cycle_sends_notification(self):
        asyncio.run(self._test_monitoring_cycle_sends_notification_async())


class TestStateMonitorGuards(unittest.TestCase):
    """Тесты гардов handle_threshold_violation."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        self.state_monitor = StateMonitor(self.db_mock)

        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"
        self.sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        self.violations = ["Value 55.0 exceeds max threshold 50.0"]

    async def _test_guard_suppress_alarm_when_maintenance_async(self):
        """Гард 1: maintenance подавляет тревогу"""
        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                operational_status=OperationalStatus.MAINTENANCE,
            ))
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations,
                sensor_code="temperature",
            )

            repo_instance.update_operational_status.assert_not_called()

    def test_guard_suppress_alarm_when_maintenance(self):
        """Гард 1: maintenance подавляет тревогу"""
        asyncio.run(self._test_guard_suppress_alarm_when_maintenance_async())

    async def _test_guard_suppress_alarm_when_door_open_for_door_sensor_async(self):
        """Гард 2: дверной датчик при открытой двери подавляется"""
        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                door=DoorState.OPEN,
            ))
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations,
                sensor_code="door",
            )

            repo_instance.update_operational_status.assert_not_called()

    def test_guard_suppress_alarm_when_door_open_for_door_sensor(self):
        """Гард 2: дверной датчик при открытой двери подавляется"""
        asyncio.run(self._test_guard_suppress_alarm_when_door_open_for_door_sensor_async())

    async def _test_guard_suppress_alarm_when_offline_async(self):
        """Гард 3: offline шкаф подавляет тревогу"""
        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                connectivity=Connectivity.OFFLINE,
            ))
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations,
                sensor_code="temperature",
            )

            repo_instance.update_operational_status.assert_not_called()

    def test_guard_suppress_alarm_when_offline(self):
        """Гард 3: offline шкаф подавляет тревогу"""
        asyncio.run(self._test_guard_suppress_alarm_when_offline_async())

    async def _test_alarm_fires_non_door_sensor_when_door_open_async(self):
        """Не-дверной датчик при открытой двери НЕ подавляется"""
        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                door=DoorState.OPEN,
            ))
            repo_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations,
                sensor_code="temperature",
            )

            repo_instance.update_operational_status.assert_called_once_with(
                self.locker_id, OperationalStatus.ALARM,
            )

    def test_alarm_fires_non_door_sensor_when_door_open(self):
        """Не-дверной датчик при открытой двери НЕ подавляется"""
        asyncio.run(self._test_alarm_fires_non_door_sensor_when_door_open_async())


class TestStateMonitorQueryFilters(unittest.TestCase):
    """Тесты фильтрации SQL-запроса мониторинга по состоянию шкафа."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.db_mock.execute = AsyncMock()
        self.state_monitor = StateMonitor(self.db_mock)

    async def _test_monitoring_query_filters_door_and_maintenance_async(self):
        """SQL-запрос мониторинга подавляет ТОЛЬКО door-сенсор при открытой двери
        и фильтрует шкафы на обслуживании.

        COR-01: широкий предикат `l.door != 'open'` исключал ВСЕ датчики любого
        шкафа с открытой дверью (слепое окно для tamper/intrusion/temperature).
        Корректный предикат сужает подавление до строки именно door-сенсора.
        """
        task = asyncio.create_task(
            self.state_monitor.start_monitoring(check_interval=0.1)
        )
        await asyncio.sleep(0.15)
        self.state_monitor.stop_monitoring()
        await task

        self.db_mock.fetch_all.assert_called()
        call_args = self.db_mock.fetch_all.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        self.assertIn("INNER JOIN lockers l ON s.locker_id = l.id", sql)
        # Подавление двери сужено до строки door-сенсора, а не всего шкафа.
        self.assertIn("stype.code = %s", sql)
        self.assertIn("l.door = %s", sql)
        # Широкого предиката `l.door != %s` больше нет — иначе он гасил бы
        # все датчики шкафа с открытой дверью.
        self.assertNotIn("l.door != %s", sql)
        # COR-14: шкафы в MAINTENANCE и ALARM исключены из sweep'а одним NOT IN.
        self.assertIn("l.operational_status NOT IN", sql)
        self.assertEqual(
            params,
            (
                "door",
                DoorState.OPEN.value,
                OperationalStatus.MAINTENANCE.value,
                OperationalStatus.ALARM.value,
            ),
        )

    def test_monitoring_query_filters_door_and_maintenance(self):
        """SQL-запрос мониторинга фильтрует шкафы с открытой дверью и на обслуживании."""
        asyncio.run(self._test_monitoring_query_filters_door_and_maintenance_async())

    async def _test_sweep_alarms_non_door_sensor_when_door_open_async(self):
        """SQL-путь: tamper/temperature в нарушении на шкафу с ОТКРЫТОЙ дверью
        ДОЛЖЕН поднять alarm; door-сенсор в нарушении на том же шкафу — подавлен.

        Это закрывает test gap: существующий не-дверной тест вызывает
        handle_threshold_violation напрямую, минуя SQL. Здесь мы прогоняем
        фактический sweep (start_monitoring -> fetch_all -> evaluate ->
        handle_threshold_violation) с замоканной БД, возвращающей строки,
        которые при корректном SQL уже НЕ отфильтрованы для не-дверных датчиков.
        """
        tamper_row = {
            "sensor_id": "660e8400-e29b-41d4-a716-446655440001",
            "locker_id": "550e8400-e29b-41d4-a716-446655440000",
            "current_value": 1.0,
            "sensor_code": "tamper",
            "sensor_type_name": "Вскрытие",
            "serial_number": "SN-TAMP-001",
            "threshold_id": 1,
            "min_value": None,
            "max_value": 0.0,
            "fixed_value": None,
        }
        # door-сенсор в нарушении на шкафу с открытой дверью: корректный SQL
        # его НЕ вернёт (исключён предикатом NOT (code='door' AND door='open')).
        self.db_mock.fetch_all.return_value = [tamper_row]

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as locker_mock:
            locker_instance = locker_mock.return_value
            # Дверь шкафа открыта — раньше это глушило tamper-сенсор в SQL.
            locker_instance.get_by_id = AsyncMock(
                return_value=_make_mock_locker(door=DoorState.OPEN),
            )
            locker_instance.update_operational_status = AsyncMock()

            self.state_monitor._send_notification = AsyncMock()
            self.state_monitor._log_alarm_event = AsyncMock()

            task = asyncio.create_task(
                self.state_monitor.start_monitoring(check_interval=0.1)
            )
            await asyncio.sleep(0.15)
            self.state_monitor.stop_monitoring()
            await task

            # tamper-датчик при открытой двери ДОЛЖЕН поднять тревогу.
            locker_instance.update_operational_status.assert_called_with(
                "550e8400-e29b-41d4-a716-446655440000",
                OperationalStatus.ALARM,
            )

    def test_sweep_alarms_non_door_sensor_when_door_open(self):
        """SQL-путь: tamper при открытой двери поднимает alarm (COR-01)."""
        asyncio.run(self._test_sweep_alarms_non_door_sensor_when_door_open_async())


class TestStateMonitorAlarmDedup(unittest.TestCase):
    """COR-14: дедупликация повторного ALARM для уже-активного нарушения.

    Шкаф, уже находящийся в ALARM из-за непрерывного нарушения (например,
    залипший tamper-датчик), НЕ должен на каждом тике sweep'а заново слать
    persistent-уведомление и security event. operational_status='alarm' сам
    служит маркером активного нарушения; пере-взвод (re-arm) происходит, когда
    оператор сбрасывает шкаф обратно в NORMAL — следующее настоящее нарушение
    тогда снова срабатывает.

    Дизайн A (намеренно грубый): пока шкаф в ALARM, нарушение НА ЛЮБОМ датчике
    подавляется, даже если это новый датчик. Шкаф уже помечен тревогой и ждёт
    инспекции оператором; повторный флуд уведомлений/событий не нужен.
    """

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        self.state_monitor = StateMonitor(self.db_mock)

        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"
        self.sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        self.violations = ["Value 55.0 exceeds max threshold 50.0"]

    async def _persisting_violation_fires_once_async(self):
        """(a) Два последовательных прохода по тому же непрерывному нарушению →
        notify + security event ровно ОДИН раз, не дважды."""
        send_mock = AsyncMock()
        log_mock = AsyncMock()
        self.state_monitor._send_notification = send_mock
        self.state_monitor._log_alarm_event = log_mock

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.update_operational_status = AsyncMock()

            # Проход 1: шкаф ещё NORMAL → нарушение срабатывает.
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                operational_status=OperationalStatus.NORMAL,
            ))
            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations,
                sensor_code="tamper",
            )

            # Проход 2: то же непрерывное нарушение, но шкаф уже в ALARM
            # (результат прохода 1) → должно быть подавлено.
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                operational_status=OperationalStatus.ALARM,
            ))
            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations,
                sensor_code="tamper",
            )

        send_mock.assert_awaited_once()
        log_mock.assert_awaited_once()
        # update_operational_status вызван ровно один раз — на первом проходе.
        repo_instance.update_operational_status.assert_awaited_once_with(
            self.locker_id, OperationalStatus.ALARM,
        )

    def test_persisting_violation_fires_once(self):
        asyncio.run(self._persisting_violation_fires_once_async())

    async def _rearm_after_operator_reset_async(self):
        """(b) После сброса оператором (NORMAL) новое нарушение СНОВА
        срабатывает — подавление не вечное."""
        send_mock = AsyncMock()
        log_mock = AsyncMock()
        self.state_monitor._send_notification = send_mock
        self.state_monitor._log_alarm_event = log_mock

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.update_operational_status = AsyncMock()

            # Нарушение #1 (NORMAL → ALARM).
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                operational_status=OperationalStatus.NORMAL,
            ))
            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations, sensor_code="tamper",
            )

            # Оператор сбросил шкаф в NORMAL; датчик снова нарушает →
            # должно сработать повторно (re-arm).
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                operational_status=OperationalStatus.NORMAL,
            ))
            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations, sensor_code="tamper",
            )

        self.assertEqual(send_mock.await_count, 2)
        self.assertEqual(log_mock.await_count, 2)
        self.assertEqual(repo_instance.update_operational_status.await_count, 2)

    def test_rearm_after_operator_reset(self):
        asyncio.run(self._rearm_after_operator_reset_async())

    async def _first_violation_still_fires_async(self):
        """(c) Первое нарушение (шкаф NORMAL) по-прежнему срабатывает — без регресса."""
        send_mock = AsyncMock()
        log_mock = AsyncMock()
        self.state_monitor._send_notification = send_mock
        self.state_monitor._log_alarm_event = log_mock

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                operational_status=OperationalStatus.NORMAL,
            ))
            repo_instance.update_operational_status = AsyncMock()

            await self.state_monitor.handle_threshold_violation(
                self.locker_id, self.sensor_id, self.violations, sensor_code="tamper",
            )

        send_mock.assert_awaited_once()
        log_mock.assert_awaited_once()
        repo_instance.update_operational_status.assert_awaited_once_with(
            self.locker_id, OperationalStatus.ALARM,
        )

    def test_first_violation_still_fires(self):
        asyncio.run(self._first_violation_still_fires_async())

    async def _new_sensor_on_alarmed_locker_suppressed_async(self):
        """(d) Дизайн A — намеренно грубый: новый датчик на уже-тревожном шкафу
        подавляется. Документируем поведение: шкаф уже помечен ALARM, оператор
        разберётся при инспекции; повторного флуда быть не должно."""
        send_mock = AsyncMock()
        log_mock = AsyncMock()
        self.state_monitor._send_notification = send_mock
        self.state_monitor._log_alarm_event = log_mock

        other_sensor_id = "660e8400-e29b-41d4-a716-44665544FFFF"

        with patch('app.locker_data.db.repositories.locker.LockerRepository') as repo_mock:
            repo_instance = repo_mock.return_value
            repo_instance.update_operational_status = AsyncMock()
            # Шкаф уже в ALARM (из-за датчика X).
            repo_instance.get_by_id = AsyncMock(return_value=_make_mock_locker(
                operational_status=OperationalStatus.ALARM,
            ))

            # Новый датчик Y нарушает порог — подавляется (Design A).
            await self.state_monitor.handle_threshold_violation(
                self.locker_id, other_sensor_id, self.violations, sensor_code="intrusion",
            )

        send_mock.assert_not_awaited()
        log_mock.assert_not_awaited()
        repo_instance.update_operational_status.assert_not_awaited()

    def test_new_sensor_on_alarmed_locker_suppressed(self):
        asyncio.run(self._new_sensor_on_alarmed_locker_suppressed_async())

    async def _sweep_excludes_already_alarmed_lockers_async(self):
        """SQL sweep исключает шкафы уже в ALARM — не вытягивает их строки
        вовсе (дедупликация на уровне запроса, экономит работу)."""
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        task = asyncio.create_task(
            self.state_monitor.start_monitoring(check_interval=0.1)
        )
        await asyncio.sleep(0.15)
        self.state_monitor.stop_monitoring()
        await task

        self.db_mock.fetch_all.assert_called()
        call_args = self.db_mock.fetch_all.call_args
        sql = call_args[0][0]
        params = call_args[0][1]
        # Шкафы в MAINTENANCE и ALARM исключены из sweep'а.
        self.assertIn("l.operational_status NOT IN", sql)
        self.assertIn(OperationalStatus.MAINTENANCE.value, params)
        self.assertIn(OperationalStatus.ALARM.value, params)

    def test_sweep_excludes_already_alarmed_lockers(self):
        asyncio.run(self._sweep_excludes_already_alarmed_lockers_async())


class TestLogAlarmEvent(unittest.TestCase):
    """Тесты _log_alarm_event: маршрутизация по sensor_type_code.

    Alarm-флоу шлёт событие через outbound queue; тип события
    (THRESHOLD_VIOLATION / INTRUSION / DOOR_TAMPER) выбирается по
    `sensor_type_code` датчика.
    """

    def setUp(self):
        self.db_mock = MagicMock()
        self.state_monitor = StateMonitor(self.db_mock)
        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"
        self.sensor_id = "660e8400-e29b-41d4-a716-446655440001"
        self.violations = ["Value 55.0 exceeds max threshold 50.0"]

    def _patch_enqueue(self):
        return patch.object(
            __import__(
                "app.services.history_producers._base", fromlist=["BaseHistoryProducer"],
            ).BaseHistoryProducer,
            "enqueue",
            new_callable=AsyncMock,
            return_value=1,
        )

    async def _threshold_violation_default_async(self):
        with self._patch_enqueue() as mock_enqueue:
            await self.state_monitor._log_alarm_event(
                self.locker_id, self.sensor_id, self.violations,
                sensor_type_code="temperature",
            )
        mock_enqueue.assert_awaited_once()
        payload = mock_enqueue.await_args.args[0]
        self.assertEqual(payload["event_type"], "threshold_violation")
        self.assertEqual(payload["severity"], "high")
        self.assertEqual(payload["sensor_id"], self.sensor_id)
        self.assertEqual(payload["locker_id"], self.locker_id)
        self.assertEqual(
            payload["violated_thresholds"],
            [{"description": self.violations[0]}],
        )
        self.assertEqual(payload["metadata"]["sensor_type_code"], "temperature")

    def test_default_sensor_type_routes_to_threshold_violation(self):
        asyncio.run(self._threshold_violation_default_async())

    async def _intrusion_routing_async(self):
        with self._patch_enqueue() as mock_enqueue:
            await self.state_monitor._log_alarm_event(
                self.locker_id, self.sensor_id, self.violations,
                sensor_type_code="intrusion",
            )
        payload = mock_enqueue.await_args.args[0]
        self.assertEqual(payload["event_type"], "intrusion")
        self.assertEqual(payload["severity"], "high")

    def test_intrusion_sensor_routes_to_intrusion_event(self):
        asyncio.run(self._intrusion_routing_async())

    async def _tamper_routing_async(self):
        with self._patch_enqueue() as mock_enqueue:
            await self.state_monitor._log_alarm_event(
                self.locker_id, self.sensor_id, self.violations,
                sensor_type_code="tamper",
            )
        payload = mock_enqueue.await_args.args[0]
        self.assertEqual(payload["event_type"], "door_tamper")
        self.assertEqual(payload["severity"], "critical")

    def test_tamper_sensor_routes_to_door_tamper(self):
        asyncio.run(self._tamper_routing_async())

    async def _no_sensor_type_falls_back_to_threshold_violation_async(self):
        with self._patch_enqueue() as mock_enqueue:
            await self.state_monitor._log_alarm_event(
                self.locker_id, self.sensor_id, self.violations,
            )
        payload = mock_enqueue.await_args.args[0]
        self.assertEqual(payload["event_type"], "threshold_violation")

    def test_no_sensor_type_falls_back_to_threshold_violation(self):
        asyncio.run(self._no_sensor_type_falls_back_to_threshold_violation_async())


if __name__ == '__main__':
    unittest.main()
