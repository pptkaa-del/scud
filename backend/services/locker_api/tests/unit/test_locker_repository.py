"""Unit-тесты LockerRepository.update_door — last_opened_at и who_open_last
обновляются только при переводе двери в OPEN, оба синхронны по конструкции."""

import asyncio
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.locker_data.db.models.locker import (
    Connectivity,
    DoorState,
    OperationalStatus,
)
from app.locker_data.db.repositories.locker import LockerRepository


def _make_row(**overrides) -> dict:
    base = {
        "id": "550e8400-e29b-41d4-a716-446655440000",
        "inventory_number": "СО №777",
        "location_address": None,
        "location_lat": None,
        "location_lon": None,
        "public_key": None,
        "created_at": datetime.now(),
        "created_by": None,
        "updated_at": datetime.now(),
        "updated_by": None,
        "connectivity": Connectivity.ONLINE,
        "door": DoorState.CLOSED,
        "operational_status": OperationalStatus.NORMAL,
        "last_seen_at": None,
        "last_opened_at": None,
        "who_open_last": None,
        "description": None,
        "metadata": {},
    }
    base.update(overrides)
    return base


class TestUpdateDoorLastOpenedAt(unittest.TestCase):
    """update_door(OPEN) должен атомарно проставлять last_opened_at = now();
    update_door(CLOSED) — не трогать last_opened_at."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.repo = LockerRepository(self.db_mock)
        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"

    async def _open_async(self):
        opened_at = datetime.now()
        self.db_mock.fetch_one.return_value = _make_row(
            door=DoorState.OPEN, last_opened_at=opened_at
        )
        result = await self.repo.update_door(self.locker_id, DoorState.OPEN)

        # Проверяем SQL: должен включать last_opened_at = now()
        sql = self.db_mock.fetch_one.call_args[0][0]
        self.assertIn("last_opened_at = now()", sql)
        # И вернувшийся объект содержит непустой last_opened_at
        self.assertEqual(result.door, DoorState.OPEN)
        self.assertEqual(result.last_opened_at, opened_at)

    def test_update_door_open_sets_last_opened_at(self):
        asyncio.run(self._open_async())

    async def _close_async(self):
        # Шкаф уже когда-то открывали — значение должно сохраниться
        previously_opened = datetime(2024, 1, 1, 12, 0, 0)
        self.db_mock.fetch_one.return_value = _make_row(
            door=DoorState.CLOSED, last_opened_at=previously_opened
        )
        result = await self.repo.update_door(self.locker_id, DoorState.CLOSED)

        sql = self.db_mock.fetch_one.call_args[0][0]
        # При CLOSED last_opened_at в UPDATE НЕ участвует
        self.assertNotIn("last_opened_at", sql)
        # Возвращённый объект сохраняет прежнее значение
        self.assertEqual(result.door, DoorState.CLOSED)
        self.assertEqual(result.last_opened_at, previously_opened)

    def test_update_door_closed_does_not_touch_last_opened_at(self):
        asyncio.run(self._close_async())

    async def _closed_sql_excludes_who_open_last_async(self):
        """Drift-guard: интеграционный helper `_set_door_closed` в
        test_who_open_last.py воспроизводит SQL CLOSED-ветки и НЕ пишет
        who_open_last. Если кто-то когда-то добавит `who_open_last = ...` в
        CLOSED-ветку update_door, integration-помощник тихо разойдётся с
        реальностью и тест test_close_preserves_who_open_last начнёт лгать.
        Этот unit-тест ловит расхождение на уровне SQL-строки."""
        self.db_mock.fetch_one.return_value = _make_row(door=DoorState.CLOSED)
        await self.repo.update_door(self.locker_id, DoorState.CLOSED)
        sql = self.db_mock.fetch_one.call_args.args[0]
        self.assertNotIn("who_open_last", sql)

    def test_update_door_closed_sql_does_not_contain_who_open_last(self):
        asyncio.run(self._closed_sql_excludes_who_open_last_async())


class TestUpdateDoorWhoOpenLast(unittest.TestCase):
    """update_door(OPEN, who_open_last=user_id) пишет переданное значение
    одной транзакцией с last_opened_at (always-overwrite). При CLOSED — не
    трогает who_open_last (информация о последнем открывшем сохраняется)."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.repo = LockerRepository(self.db_mock)
        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"

    async def _open_with_user_async(self):
        self.db_mock.fetch_one.return_value = _make_row(
            door=DoorState.OPEN, last_opened_at=datetime.now(), who_open_last=42
        )
        result = await self.repo.update_door(
            self.locker_id, DoorState.OPEN, who_open_last=42
        )

        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]
        # SQL обновляет who_open_last вместе с last_opened_at
        self.assertIn("who_open_last = %s", sql)
        self.assertIn("last_opened_at = now()", sql)
        # User id передан в параметрах между door и locker_id;
        # 'active'-маркер (COR-17) замыкает params.
        self.assertEqual(params, (DoorState.OPEN, 42, self.locker_id, "active"))
        self.assertEqual(result.who_open_last, 42)

    def test_update_door_open_with_user_sets_who_open_last(self):
        asyncio.run(self._open_with_user_async())

    async def _open_without_user_async(self):
        # System-issued OPEN (created_by=None) — always-overwrite: None уходит в SQL
        self.db_mock.fetch_one.return_value = _make_row(
            door=DoorState.OPEN, last_opened_at=datetime.now(), who_open_last=None
        )
        result = await self.repo.update_door(self.locker_id, DoorState.OPEN)

        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]
        self.assertIn("who_open_last = %s", sql)
        # COALESCE/CASE-обходов нет — пишем дословно переданное значение
        self.assertNotIn("COALESCE", sql)
        self.assertEqual(params, (DoorState.OPEN, None, self.locker_id, "active"))
        self.assertIsNone(result.who_open_last)

    def test_update_door_open_without_user_writes_none(self):
        asyncio.run(self._open_without_user_async())

    async def _closed_skips_who_open_last_async(self):
        self.db_mock.fetch_one.return_value = _make_row(
            door=DoorState.CLOSED, who_open_last=11
        )
        await self.repo.update_door(self.locker_id, DoorState.CLOSED, who_open_last=99)

        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]
        # CLOSED-ветка не трогает who_open_last
        self.assertNotIn("who_open_last", sql)
        # И не должна тащить лишний параметр в плейсхолдеры
        # (door, locker_id, 'active'-маркер COR-17)
        self.assertEqual(params, (DoorState.CLOSED, self.locker_id, "active"))

    def test_update_door_closed_does_not_touch_who_open_last(self):
        asyncio.run(self._closed_skips_who_open_last_async())


class TestSyncDoorState(unittest.TestCase):
    """sync_door_state — ТЕЛЕМЕТРИЧЕСКИЙ путь (ingest heartbeat/stat).

    В отличие от командного update_door, обновляет ТОЛЬКО door (+updated_at),
    edge-triggered (`door <> %s` — пишет лишь при реальной смене состояния) и
    НИКОГДА не трогает last_opened_at / who_open_last. Регрессия бага «дверь
    сама переоткрывается каждые ~5 минут»: heartbeat-stat с залипшим door=open
    гонял командный update_door(OPEN), который сбрасывал last_opened_at=now() и
    затирал who_open_last=NULL на каждый heartbeat (подтверждено на стенде)."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.repo = LockerRepository(self.db_mock)
        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"

    async def _sync_async(self, door):
        self.db_mock.fetch_one.return_value = _make_row(door=door)
        result = await self.repo.sync_door_state(self.locker_id, door)
        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]
        kwargs = self.db_mock.fetch_one.call_args.kwargs
        # Открывающие метаданные владеет ТОЛЬКО командный путь — телеметрия их не пишет.
        self.assertNotIn("last_opened_at", sql)
        self.assertNotIn("who_open_last", sql)
        # Edge-trigger: апдейт срабатывает лишь при смене состояния.
        self.assertIn("door <> %s", sql)
        # Паритет COR-17: архивные шкафы не мутируются.
        self.assertIn("state = %s", sql)
        # params: door в SET и в WHERE (door<>), плюс locker_id и 'active'-маркер.
        self.assertEqual(params, (door, self.locker_id, "active", door))
        # Это write.
        self.assertEqual(kwargs.get("read_only"), False)
        return result

    def test_sync_door_state_open_excludes_open_metadata(self):
        result = asyncio.run(self._sync_async(DoorState.OPEN))
        self.assertEqual(result.door, DoorState.OPEN)

    def test_sync_door_state_closed_excludes_open_metadata(self):
        asyncio.run(self._sync_async(DoorState.CLOSED))

    async def _no_change_async(self):
        # Edge-trigger не сматчил (door уже == новому) → 0 строк → None.
        self.db_mock.fetch_one.return_value = None
        return await self.repo.sync_door_state(self.locker_id, DoorState.OPEN)

    def test_sync_door_state_no_change_returns_none(self):
        self.assertIsNone(asyncio.run(self._no_change_async()))


class TestUpdateDoorNotFound(unittest.TestCase):
    """update_door возвращает None если локер не найден (например, архивный
    или вообще не существует) — и при этом не падает на новом kwarg who_open_last."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.repo = LockerRepository(self.db_mock)
        self.missing_locker_id = "00000000-0000-0000-0000-000000000000"

    async def _open_async(self):
        self.db_mock.fetch_one.return_value = None
        result = await self.repo.update_door(
            self.missing_locker_id, DoorState.OPEN, who_open_last=42
        )
        self.assertIsNone(result)
        # SQL всё равно должен был выполниться с переданными params (UPDATE
        # просто не нашёл строку), включая who_open_last=42.
        params = self.db_mock.fetch_one.call_args[0][1]
        self.assertEqual(
            params, (DoorState.OPEN, 42, self.missing_locker_id, "active")
        )

    def test_update_door_open_returns_none_when_locker_missing(self):
        asyncio.run(self._open_async())

    async def _close_async(self):
        self.db_mock.fetch_one.return_value = None
        result = await self.repo.update_door(self.missing_locker_id, DoorState.CLOSED)
        self.assertIsNone(result)
        params = self.db_mock.fetch_one.call_args[0][1]
        self.assertEqual(params, (DoorState.CLOSED, self.missing_locker_id, "active"))

    def test_update_door_closed_returns_none_when_locker_missing(self):
        asyncio.run(self._close_async())


class TestGetAllLocationView(unittest.TestCase):
    """view=location SELECT включает operational_status (нужен фронтенду карты)."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.repo = LockerRepository(self.db_mock)

    async def _async_select(self):
        self.db_mock.fetch_all.return_value = [{
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "location_lat": 55.0,
            "location_lon": 37.0,
            "location_address": "A",
            "operational_status": OperationalStatus.ALARM,
        }]
        result = await self.repo.get_all(view="location")

        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertIn("operational_status", sql)
        self.assertEqual(result[0].operational_status, OperationalStatus.ALARM)

    def test_get_all_location_view_selects_operational_status(self):
        asyncio.run(self._async_select())

    async def _async_batch(self):
        self.db_mock.fetch_all.return_value = [{
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "location_lat": 55.0,
            "location_lon": 37.0,
            "location_address": "A",
            "operational_status": OperationalStatus.MAINTENANCE,
        }]
        result = await self.repo.get_batch_location(["550e8400-e29b-41d4-a716-446655440000"])

        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertIn("operational_status", sql)
        self.assertEqual(
            result["550e8400-e29b-41d4-a716-446655440000"].operational_status,
            OperationalStatus.MAINTENANCE,
        )

    def test_get_batch_location_selects_operational_status(self):
        asyncio.run(self._async_batch())


class TestUpdateOperationalStatusStateFilter(unittest.TestCase):
    """COR-17: update_operational_status не должен мутировать архивный шкаф."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.repo = LockerRepository(self.db_mock)
        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"

    async def _active_async(self):
        self.db_mock.fetch_one.return_value = _make_row(
            operational_status=OperationalStatus.MAINTENANCE
        )
        result = await self.repo.update_operational_status(
            self.locker_id, OperationalStatus.MAINTENANCE, updated_by=7
        )

        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]
        self.assertIn("state = %s", sql)
        self.assertIn("active", params)
        self.assertIsNotNone(result)
        self.assertEqual(result.operational_status, OperationalStatus.MAINTENANCE)

    def test_update_operational_status_filters_active_state(self):
        asyncio.run(self._active_async())

    async def _archived_async(self):
        self.db_mock.fetch_one.return_value = None
        result = await self.repo.update_operational_status(
            self.locker_id, OperationalStatus.MAINTENANCE, updated_by=7
        )
        self.assertIsNone(result)

    def test_update_operational_status_returns_none_for_archived(self):
        asyncio.run(self._archived_async())


class TestUpdateDoorStateFilter(unittest.TestCase):
    """COR-17: update_door не должен мутировать архивный шкаф.

    update_door вызывается из CommandResultApplier по позднему ACK устройства.
    Если шкаф архивировали (state='archived') между выдачей команды и её ACK,
    UPDATE обязан фильтровать state='active' — иначе door/last_opened_at/
    who_open_last перепишутся на архивной строке. Обе ветки (OPEN и CLOSED)
    должны содержать предикат state = %s с маркером 'active' в params."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.repo = LockerRepository(self.db_mock)
        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"

    async def _open_filters_active_async(self):
        self.db_mock.fetch_one.return_value = _make_row(
            door=DoorState.OPEN, last_opened_at=datetime.now(), who_open_last=42
        )
        result = await self.repo.update_door(
            self.locker_id, DoorState.OPEN, who_open_last=42
        )

        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]
        self.assertIn("state = %s", sql)
        self.assertIn("active", params)
        # 'active'-маркер — последний параметр (после locker_id).
        self.assertEqual(params, (DoorState.OPEN, 42, self.locker_id, "active"))
        self.assertIsNotNone(result)

    def test_update_door_open_filters_active_state(self):
        asyncio.run(self._open_filters_active_async())

    async def _closed_filters_active_async(self):
        self.db_mock.fetch_one.return_value = _make_row(door=DoorState.CLOSED)
        await self.repo.update_door(self.locker_id, DoorState.CLOSED)

        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]
        self.assertIn("state = %s", sql)
        self.assertIn("active", params)
        self.assertEqual(params, (DoorState.CLOSED, self.locker_id, "active"))

    def test_update_door_closed_filters_active_state(self):
        asyncio.run(self._closed_filters_active_async())

    async def _archived_returns_none_async(self, door):
        # Архивный шкаф не матчит state='active' → 0 строк → None.
        self.db_mock.fetch_one.return_value = None
        result = await self.repo.update_door(self.locker_id, door)
        self.assertIsNone(result)

    def test_update_door_open_returns_none_for_archived(self):
        asyncio.run(self._archived_returns_none_async(DoorState.OPEN))

    def test_update_door_closed_returns_none_for_archived(self):
        asyncio.run(self._archived_returns_none_async(DoorState.CLOSED))


class TestMarkOfflineIfOnline(unittest.TestCase):
    """COR-19: атомарный online→offline флип одним UPDATE … RETURNING.

    Возвращает True только если ЭТОТ вызов реально перевёл шкаф (row != None);
    повторный вызов на уже offline-шкафе матчит 0 строк → False."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.repo = LockerRepository(self.db_mock)
        self.locker_id = "550e8400-e29b-41d4-a716-446655440000"

    async def _won_async(self):
        # Строка вернулась → этот вызов выиграл переход.
        self.db_mock.fetch_one.return_value = {"id": self.locker_id}
        won = await self.repo.mark_offline_if_online(self.locker_id)

        sql = self.db_mock.fetch_one.call_args[0][0]
        kwargs = self.db_mock.fetch_one.call_args.kwargs
        # Один атомарный UPDATE с предикатом connectivity='online' и RETURNING.
        self.assertIn("connectivity = 'offline'", sql)
        self.assertIn("connectivity = 'online'", sql)
        self.assertIn("state = 'active'", sql)
        self.assertIn("RETURNING", sql)
        # Это write — read_only=False.
        self.assertEqual(kwargs.get("read_only"), False)
        self.assertTrue(won)

    def test_mark_offline_if_online_returns_true_when_won(self):
        asyncio.run(self._won_async())

    async def _lost_async(self):
        # 0 строк (шкаф уже offline или архивный) → False.
        self.db_mock.fetch_one.return_value = None
        won = await self.repo.mark_offline_if_online(self.locker_id)
        self.assertFalse(won)

    def test_mark_offline_if_online_returns_false_when_lost(self):
        asyncio.run(self._lost_async())


if __name__ == "__main__":
    unittest.main()
