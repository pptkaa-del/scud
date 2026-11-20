"""
Тесты cross-service инвалидации ACM cache при lifecycle-событиях user.

Verify, что invalidate_user(user_id) вызывается:
  - после DELETE /api/v1/user/{id}    (deactivate)
  - после POST   /api/v1/user/{id}/reactivate
  - после PATCH  /api/v1/user/{id} с изменением role
  - НЕ вызывается при PATCH без изменения role (только phone/name)

Используется AsyncHTTPTestCase с моком db и моком CacheInvalidator,
переданным через make_app(cache_invalidator=...). Это handler-level тест:
реальный Redis не нужен, db полностью мокается.
"""
import asyncio
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.services.cache_invalidator import CacheInvalidator
from app.user_data.db.models.user import UserState


def _user_row(user_id: int = 2, phone: str = "+79005556677",
              full_name: str = "Target User", role: str = "worker",
              state: str = UserState.ACTIVE.value) -> dict:
    return {
        "id": user_id, "phone": phone, "full_name": full_name,
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": 1,
        "updated_at": datetime.now(), "updated_by": 1,
        "state": state, "role": role,
    }


def _auth_row() -> dict:
    return {
        "id": 1, "phone": "+79123456789", "full_name": "Admin",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": None,
        "updated_at": datetime.now(), "updated_by": None,
        "state": UserState.ACTIVE.value, "role": "administrator",
    }


class TestUserLifecycleInvalidation(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()

        self.invalidator_mock = MagicMock()
        self.invalidator_mock.invalidate_user = AsyncMock()

        return make_app(self.db_mock, cache_invalidator=self.invalidator_mock)

    def setUp(self):
        super().setUp()
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.execute.reset_mock()
        self.invalidator_mock.invalidate_user.reset_mock()

    # ------------------------------------------------------------------
    # DELETE — deactivate
    # ------------------------------------------------------------------

    def test_delete_user_invokes_invalidator(self):
        """DELETE /user/{id} → invalidate_user вызывается с правильным id."""
        self.db_mock.fetch_one.return_value = _auth_row()
        response = self.fetch("/api/v1/user/42", method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)
        self.invalidator_mock.invalidate_user.assert_awaited_once_with(42)

    # ------------------------------------------------------------------
    # POST /reactivate
    # ------------------------------------------------------------------

    def test_reactivate_invokes_invalidator(self):
        """POST /user/{id}/reactivate → invalidate_user вызывается.

        repo.reactivate возвращает {id} через UPDATE...RETURNING,
        затем get_by_id(user_id) формирует финальный объект через JOIN.
        """
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),                          # prepare() auth
            {"id": 42},                           # UPDATE...RETURNING id
            _user_row(user_id=42),                # final get_by_id
        ]
        response = self.fetch("/api/v1/user/42/reactivate", method="POST",
                              body="", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.invalidator_mock.invalidate_user.assert_awaited_once_with(42)

    def test_reactivate_user_not_found_does_not_invalidate(self):
        """Если reactivate вернул None (не найден) — invalidator НЕ вызывается."""
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),  # prepare() auth
            None,         # UPDATE...RETURNING — пусто (нет user_id или уже ACTIVE)
            None,         # get_by_id fallback — нет такого пользователя
        ]
        response = self.fetch("/api/v1/user/999/reactivate", method="POST",
                              body="", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
        self.invalidator_mock.invalidate_user.assert_not_awaited()

    # ------------------------------------------------------------------
    # PATCH — role change vs other changes
    # ------------------------------------------------------------------

    def test_patch_with_role_change_invokes_invalidator(self):
        """PATCH /user/{id} с role → invalidate_user вызывается."""
        target = _user_row(user_id=2, role="worker")
        updated = _user_row(user_id=2, role="operator")
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),  # prepare() auth
            target,       # get_by_id existence check
            updated,      # get_by_id after UPDATE
        ]
        body = {"role": "operator"}
        response = self.fetch("/api/v1/user/2", method="PATCH",
                              body=json.dumps(body),
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.invalidator_mock.invalidate_user.assert_awaited_once_with(2)

    def test_patch_without_role_change_does_not_invalidate(self):
        """PATCH /user/{id} только с full_name → invalidator НЕ вызывается."""
        target = _user_row(user_id=2)
        updated = _user_row(user_id=2, full_name="Renamed")
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            target,
            updated,
        ]
        body = {"full_name": "Renamed"}
        response = self.fetch("/api/v1/user/2", method="PATCH",
                              body=json.dumps(body),
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.invalidator_mock.invalidate_user.assert_not_awaited()

    def test_patch_phone_only_does_not_invalidate(self):
        """PATCH /user/{id} только с phone → invalidator НЕ вызывается."""
        target = _user_row(user_id=2, phone="+79005556677")
        updated = _user_row(user_id=2, phone="+79007778899")
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            target,
            None,     # get_by_phone — свободно
            updated,
        ]
        body = {"phone": "+79007778899"}
        response = self.fetch("/api/v1/user/2", method="PATCH",
                              body=json.dumps(body),
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.invalidator_mock.invalidate_user.assert_not_awaited()


class TestCacheInvalidatorAbsent(AsyncHTTPTestCase):
    """Если cache_invalidator не сконфигурирован (тестовое окружение без Redis),
    handlers должны работать без ошибок."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        return make_app(self.db_mock, cache_invalidator=None)

    def test_delete_works_without_invalidator(self):
        self.db_mock.fetch_one.return_value = _auth_row()
        response = self.fetch("/api/v1/user/42", method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)


def test_cache_invalidator_calls_redis_delete():
    """CacheInvalidator → Redis DEL с правильным ключом."""
    async def run():
        redis_mock = MagicMock()
        redis_mock.delete = AsyncMock(return_value=1)
        inv = CacheInvalidator(redis_mock)
        await inv.invalidate_user(42)
        redis_mock.delete.assert_awaited_once_with("user_ctx:42")
    asyncio.run(run())


def test_cache_invalidator_swallows_redis_errors():
    """CacheInvalidator не пробрасывает Redis ConnectionError."""
    async def run():
        redis_mock = MagicMock()
        redis_mock.delete = AsyncMock(side_effect=ConnectionError("boom"))
        inv = CacheInvalidator(redis_mock)
        await inv.invalidate_user(42)  # не должно бросить
    asyncio.run(run())
