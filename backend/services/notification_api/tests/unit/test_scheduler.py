"""Unit-тесты для NotificationScheduler"""
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncTestCase, gen_test

from app.services.scheduler import NotificationScheduler


class TestNotificationScheduler(AsyncTestCase):
    def setUp(self):
        super().setUp()
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        self.scheduler = NotificationScheduler(self.db_mock)

    @gen_test
    async def test_purge_outbound_max_issues_delete(self):
        """Планировщик дергает purge_expired (SEC-18) — DELETE по outbox."""
        await self.scheduler._purge_outbound_max()

        assert self.db_mock.execute.called
        query = self.db_mock.execute.await_args.args[0]
        norm = " ".join(query.split()).lower()
        assert norm.startswith("delete from outbound_max_messages")
