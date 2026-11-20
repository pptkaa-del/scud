from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import unittest
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from app.history_data.db.models.media import MediaKind
from app.history_data.db.repositories.media_repository import MediaRepository


class TestMediaRepositoryInsert(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.fetch_one = AsyncMock(return_value={"id": 42})
        self.repo = MediaRepository(self.db)

    async def _run_insert(self):
        media_id = await self.repo.insert(
            locker_id=uuid4(),
            sensor_id=uuid4(),
            security_event_id=7,
            kind=MediaKind.PHOTO,
            mime_type="image/jpeg",
            bytes_=b"\xff\xd8\xff\xe0fake",
            captured_at=datetime.now(UTC),
            width=640,
            height=480,
            duration_ms=None,
            description=None,
            metadata={},
        )
        self.assertEqual(media_id, 42)
        sql = self.db.fetch_one.call_args[0][0]
        self.assertIn("INSERT INTO locker_media", sql)

    def test_insert_returns_id(self):
        asyncio.run(self._run_insert())


class TestMediaRepositoryListByEvent(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.fetch_all = AsyncMock(return_value=[])
        self.repo = MediaRepository(self.db)

    async def _run(self):
        items = await self.repo.list_by_event(event_id=7)
        sql = self.db.fetch_all.call_args[0][0]
        params = self.db.fetch_all.call_args[0][1]
        self.assertIn("security_event_id = %s", sql)
        self.assertIn(7, params)
        self.assertEqual(items, [])

    def test_list_by_event(self):
        asyncio.run(self._run())

    def test_list_by_event_orders_by_photo_id_then_capture_time(self):
        asyncio.run(self.repo.list_by_event(event_id=7))
        sql = " ".join(self.db.fetch_all.call_args[0][0].split())
        order = sql[sql.index("ORDER BY"):]
        self.assertEqual(
            order,
            "ORDER BY CASE WHEN jsonb_typeof(metadata -> 'photo_id') = 'number' "
            "THEN (metadata ->> 'photo_id')::numeric END ASC NULLS LAST, "
            "captured_at ASC, id ASC",
        )


class TestMediaRepositoryGetById(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.fetch_one = AsyncMock(return_value=None)
        self.repo = MediaRepository(self.db)

    async def _run(self):
        result = await self.repo.get_by_id_with_bytes(media_id=999)
        self.assertIsNone(result)

    def test_get_by_id_not_found(self):
        asyncio.run(self._run())


class TestMediaRepositoryFindEventInWindow(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.repo = MediaRepository(self.db)

    async def _run_found(self):
        self.db.fetch_one = AsyncMock(return_value={"id": 55})
        result = await self.repo.find_event_in_window(
            locker_id=uuid4(),
            sensor_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )
        self.assertEqual(result, 55)
        sql = self.db.fetch_one.call_args[0][0]
        self.assertIn("security_events", sql)
        self.assertIn("30 seconds", sql)

    async def _run_not_found(self):
        self.db.fetch_one = AsyncMock(return_value=None)
        result = await self.repo.find_event_in_window(
            locker_id=uuid4(),
            sensor_id=uuid4(),
            occurred_at=datetime.now(UTC),
        )
        self.assertIsNone(result)

    def test_find_event_in_window_found(self):
        asyncio.run(self._run_found())

    def test_find_event_in_window_skips_series_events(self):
        self.db.fetch_one = AsyncMock(return_value=None)
        asyncio.run(self.repo.find_event_in_window(
            locker_id=uuid4(), sensor_id=uuid4(), occurred_at=datetime.now(UTC),
        ))
        sql, params = self.db.fetch_one.call_args[0][:2]
        self.assertIn("NOT (COALESCE(metadata, '{}'::jsonb) ? %s)", sql)
        self.assertIn("series_uid", params)

    def test_find_event_in_window_not_found(self):
        asyncio.run(self._run_not_found())


class TestMediaRepositoryInsertSecurityEventUid(unittest.TestCase):
    def test_insert_passes_security_event_uid(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value={"id": 1})
        uid = str(uuid4())
        asyncio.run(MediaRepository(db).insert(
            locker_id=uuid4(), sensor_id=None, security_event_id=None,
            kind=MediaKind.PHOTO, mime_type="image/jpeg", bytes_=b"\xff\xd8",
            captured_at=datetime.now(UTC), width=None, height=None,
            duration_ms=None, description=None, metadata={},
            security_event_uid=uid,
        ))
        sql, params = db.fetch_one.call_args[0][:2]
        self.assertIn("security_event_uid", sql)
        self.assertEqual(sql.count("%s"), len(params))
        self.assertIn(uid, params)


class TestMediaRepositoryEventUidLinking(unittest.TestCase):
    def setUp(self):
        self.db = MagicMock()
        self.db.execute = AsyncMock()
        self.repo = MediaRepository(self.db)

    @staticmethod
    def _norm(sql: str) -> str:
        return " ".join(sql.split())

    def test_find_event_id_by_uid(self):
        self.db.fetch_one = AsyncMock(return_value={"id": 9})
        self.assertEqual(asyncio.run(self.repo.find_event_id_by_uid("u-1")), 9)
        sql, params = self.db.fetch_one.call_args[0][:2]
        self.assertIn("FROM security_events WHERE event_uid = %s", self._norm(sql))
        self.assertEqual(params, ("u-1",))

    def test_find_event_id_by_uid_miss(self):
        self.db.fetch_one = AsyncMock(return_value=None)
        self.assertIsNone(asyncio.run(self.repo.find_event_id_by_uid("u-1")))

    def test_relink_media(self):
        asyncio.run(self.repo.relink_media(5))
        args, kwargs = self.db.execute.call_args
        sql = self._norm(args[0])
        self.assertIn("UPDATE locker_media m SET security_event_id = se.id", sql)
        self.assertIn("m.id = %s", sql)
        self.assertIn("m.security_event_id IS NULL", sql)
        self.assertIn("se.event_uid = m.security_event_uid", sql)
        self.assertEqual(args[1], (5,))
        self.assertTrue(kwargs.get("idempotent"))

    def test_relink_by_media_uid(self):
        asyncio.run(self.repo.relink_by_media_uid("m-uid"))
        args, kwargs = self.db.execute.call_args
        sql = self._norm(args[0])
        self.assertIn("m.event_uid = %s", sql)
        self.assertIn("m.security_event_id IS NULL", sql)
        self.assertIn("se.event_uid = m.security_event_uid", sql)
        self.assertEqual(args[1], ("m-uid",))
        self.assertTrue(kwargs.get("idempotent"))

    def test_link_pending_to_event(self):
        asyncio.run(self.repo.link_pending_to_event("e-uid"))
        args, kwargs = self.db.execute.call_args
        sql = self._norm(args[0])
        self.assertIn("se.event_uid = %s", sql)
        self.assertIn("m.security_event_uid = se.event_uid", sql)
        self.assertIn("m.security_event_id IS NULL", sql)
        self.assertIn("m.deleted_at IS NULL", sql)
        self.assertEqual(args[1], ("e-uid",))
        self.assertTrue(kwargs.get("idempotent"))


class TestMediaRepositoryListPaginated(unittest.TestCase):
    """I3: тесты list_paginated — фильтры, COUNT, параметры."""

    def setUp(self):
        self.db = MagicMock()
        self.repo = MediaRepository(self.db)

    # a) все фильтры None → только deleted_at IS NULL, лишних params нет
    async def _run_no_filters(self):
        self.db.fetch_one = AsyncMock(return_value={"c": 0})
        self.db.fetch_all = AsyncMock(return_value=[])
        items, total = await self.repo.list_paginated(
            locker_id=None,
            kind=None,
            from_dt=None,
            to_dt=None,
            security_event_id=None,
            limit=10,
            offset=0,
        )
        count_sql = self.db.fetch_one.call_args[0][0]
        count_params = self.db.fetch_one.call_args[0][1]
        self.assertIn("deleted_at IS NULL", count_sql)
        self.assertNotIn("locker_id", count_sql)
        self.assertNotIn("kind", count_sql)
        self.assertNotIn("captured_at", count_sql)
        self.assertNotIn("security_event_id", count_sql)
        # лишних params нет: только limit/offset переданы в list_sql
        self.assertEqual(len(count_params), 0)
        self.assertEqual(items, [])
        self.assertEqual(total, 0)

    def test_no_filters(self):
        asyncio.run(self._run_no_filters())

    # b) все 5 фильтров заданы → все 5 условий в WHERE, все 5 params
    async def _run_all_filters(self):
        locker = uuid4()
        from_dt = datetime(2026, 1, 1, tzinfo=UTC)
        to_dt = datetime(2026, 6, 1, tzinfo=UTC)
        self.db.fetch_one = AsyncMock(return_value={"c": 2})
        self.db.fetch_all = AsyncMock(return_value=[])
        items, total = await self.repo.list_paginated(
            locker_id=locker,
            kind=MediaKind.PHOTO,
            from_dt=from_dt,
            to_dt=to_dt,
            security_event_id=42,
            limit=5,
            offset=0,
        )
        count_sql = self.db.fetch_one.call_args[0][0]
        count_params = self.db.fetch_one.call_args[0][1]
        self.assertIn("locker_id = %s", count_sql)
        self.assertIn("kind = %s", count_sql)
        self.assertIn("captured_at >= %s", count_sql)
        self.assertIn("captured_at <= %s", count_sql)
        self.assertIn("security_event_id = %s", count_sql)
        self.assertEqual(len(count_params), 5)
        self.assertEqual(total, 2)

    def test_all_filters(self):
        asyncio.run(self._run_all_filters())

    # c) total берётся из COUNT-запроса
    async def _run_total_from_count(self):
        self.db.fetch_one = AsyncMock(return_value={"c": 99})
        self.db.fetch_all = AsyncMock(return_value=[])
        _, total = await self.repo.list_paginated(
            locker_id=None,
            kind=None,
            from_dt=None,
            to_dt=None,
            security_event_id=None,
            limit=10,
            offset=0,
        )
        self.assertEqual(total, 99)

    def test_total_from_count(self):
        asyncio.run(self._run_total_from_count())


if __name__ == "__main__":
    unittest.main()
