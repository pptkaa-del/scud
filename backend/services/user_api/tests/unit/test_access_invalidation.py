"""Тесты инвалидации ACM cache при изменении допусков (SEC-15).

При создании / деактивации / реактивации допуска пользователя ACM-кэш
контекста (user_ctx:{id}, TTL ~30s) обязан инвалидироваться немедленно —
иначе отозванный допуск авторизует ещё до 30 секунд.

Mirror на test_user_lifecycle.py: AsyncHTTPTestCase, db и CacheInvalidator
мокаются, реальный Redis не нужен.
"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.user import UserState


def _auth_row() -> dict:
    return {
        "id": 1, "phone": "+79123456789", "full_name": "Admin",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": None,
        "updated_at": datetime.now(), "updated_by": None,
        "state": UserState.ACTIVE.value, "role": "administrator",
    }


def _user_row(user_id: int = 7) -> dict:
    return {
        "id": user_id, "phone": "+79005556677", "full_name": "Target",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": 1,
        "updated_at": datetime.now(), "updated_by": 1,
        "state": UserState.ACTIVE.value, "role": "worker",
    }


def _access_row(access_id: int = 5, user_id: int = 7,
                is_active: bool = True) -> dict:
    return {
        "id": access_id, "user_id": user_id,
        "start_at": datetime.now(), "end_at": None,
        "granted_by": 1, "granted_by_full_name": "Admin",
        "request_id": None, "full_access": True,
        "is_active": is_active, "created_at": datetime.now(),
    }


class TestAccessInvalidation(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.db_mock.execute = AsyncMock()

        self.invalidator_mock = MagicMock()
        self.invalidator_mock.invalidate_user = AsyncMock()

        return make_app(self.db_mock, cache_invalidator=self.invalidator_mock)

    def setUp(self):
        super().setUp()
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.fetch_one_strict.reset_mock()
        self.db_mock.execute.reset_mock()
        self.invalidator_mock.invalidate_user.reset_mock()

    def test_create_grant_invokes_invalidator(self):
        """POST /user/{id}/accesses → invalidate_user(user_id)."""
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),            # prepare() auth
            _user_row(user_id=7),   # user existence get_by_id
            _access_row(user_id=7),  # get_by_id after create
        ]
        self.db_mock.fetch_one_strict.return_value = {"id": 5}
        body = {"start_at": "2026-01-01T00:00:00Z", "full_access": True}
        response = self.fetch("/api/v1/user/7/accesses", method="POST",
                              body=json.dumps(body),
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 201)
        self.invalidator_mock.invalidate_user.assert_awaited_once_with(7)

    def test_revoke_grant_invokes_invalidator(self):
        """DELETE /access/{id} → invalidate_user(user_id допуска)."""
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),                                # prepare() auth
            _access_row(access_id=5, user_id=7),        # deactivate existence
            _access_row(access_id=5, user_id=7, is_active=False),  # final get_by_id
        ]
        response = self.fetch("/api/v1/access/5", method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)
        self.invalidator_mock.invalidate_user.assert_awaited_once_with(7)

    def test_reactivate_grant_invokes_invalidator(self):
        """POST /access/{id}/reactivate → invalidate_user(user_id допуска)."""
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),                                          # prepare() auth
            _access_row(access_id=5, user_id=7, is_active=False),  # reactivate existence
            _access_row(access_id=5, user_id=7),                  # final get_by_id
        ]
        response = self.fetch("/api/v1/access/5/reactivate", method="POST",
                              body="", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.invalidator_mock.invalidate_user.assert_awaited_once_with(7)

    def test_revoke_not_found_does_not_invalidate(self):
        """DELETE несуществующего допуска → invalidator НЕ вызывается."""
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),  # prepare() auth
            None,         # deactivate existence — нет такого допуска
        ]
        response = self.fetch("/api/v1/access/999", method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
        self.invalidator_mock.invalidate_user.assert_not_awaited()
