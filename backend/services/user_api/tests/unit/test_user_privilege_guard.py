"""Unit-тесты защиты от эскалации привилегий (SEC-01 / SEC-03).

Два уровня:

1. Чистая матрица ``shared.access_control.user_management`` (caller-роль ↔
   target-роль ↔ назначаемая роль). Тестируется в изоляции.
2. Handler-level guard в user_api: PATCH /api/v1/user/{id}, enable-2fa,
   disable-2fa. DB мокается; caller-роль приходит через X-Remote-Role.

Покрытие сценариев атаки:
- operator self-promote → administrator → 403
- operator ставит role=administrator другому → 403
- operator сбрасывает пароль администратору → 403
- operator снимает 2FA с администратора → 403
- operator меняет телефон/роль worker'а → 200/OK
- administrator повышает worker'а до operator → 200/OK
"""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import unittest
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.user import UserState
from shared.access_control import (
    can_assign_role,
    can_modify_target,
    is_self_promotion,
)


# ===========================================================================
# 1. Чистая матрица user_management
# ===========================================================================
class TestRoleMatrix(unittest.TestCase):
    # can_assign_role — правило 1 (нельзя назначить роль выше своей)
    def test_operator_cannot_assign_administrator(self):
        self.assertFalse(can_assign_role("operator", "administrator"))

    def test_operator_can_assign_operator(self):
        self.assertTrue(can_assign_role("operator", "operator"))

    def test_operator_can_assign_worker(self):
        self.assertTrue(can_assign_role("operator", "worker"))

    def test_administrator_can_assign_administrator(self):
        self.assertTrue(can_assign_role("administrator", "administrator"))

    def test_worker_cannot_assign_operator(self):
        self.assertFalse(can_assign_role("worker", "operator"))

    def test_empty_caller_role_denied(self):
        self.assertFalse(can_assign_role("", "worker"))
        self.assertFalse(can_assign_role(None, "worker"))

    def test_unknown_new_role_denied(self):
        self.assertFalse(can_assign_role("administrator", "superadmin"))

    # can_modify_target — правило 2 (нельзя трогать target >= своей роли)
    def test_operator_cannot_modify_administrator(self):
        self.assertFalse(can_modify_target("operator", "administrator"))

    def test_operator_cannot_modify_operator(self):
        self.assertFalse(can_modify_target("operator", "operator"))

    def test_operator_can_modify_worker(self):
        self.assertTrue(can_modify_target("operator", "worker"))

    def test_administrator_can_modify_administrator(self):
        self.assertTrue(can_modify_target("administrator", "administrator"))

    def test_administrator_can_modify_operator(self):
        self.assertTrue(can_modify_target("administrator", "operator"))

    def test_empty_caller_cannot_modify(self):
        self.assertFalse(can_modify_target("", "worker"))
        self.assertFalse(can_modify_target(None, "worker"))

    def test_non_admin_cannot_modify_unknown_role_target(self):
        self.assertFalse(can_modify_target("operator", "superadmin"))

    # is_self_promotion — правило 3
    def test_self_promotion_detected(self):
        self.assertTrue(
            is_self_promotion("operator", "administrator", caller_is_target=True)
        )

    def test_self_keep_same_role_not_promotion(self):
        self.assertFalse(
            is_self_promotion("operator", "operator", caller_is_target=True)
        )

    def test_self_demote_not_promotion(self):
        self.assertFalse(
            is_self_promotion("operator", "worker", caller_is_target=True)
        )

    def test_promote_other_not_self_promotion(self):
        self.assertFalse(
            is_self_promotion("operator", "administrator", caller_is_target=False)
        )

    def test_no_role_change_not_promotion(self):
        self.assertFalse(
            is_self_promotion("operator", None, caller_is_target=True)
        )


# ===========================================================================
# 2. Handler-level guard
# ===========================================================================
def _admin_auth_row(user_id: int = 1) -> dict:
    return {
        "id": user_id, "phone": "+79123456789", "full_name": "Admin",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": None,
        "updated_at": datetime.now(), "updated_by": None,
        "state": UserState.ACTIVE.value, "role": "administrator",
        "max_user_id": None,
    }


def _operator_auth_row(user_id: int = 5) -> dict:
    return {
        "id": user_id, "phone": "+79123456000", "full_name": "Operator",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": None,
        "updated_at": datetime.now(), "updated_by": None,
        "state": UserState.ACTIVE.value, "role": "operator",
        "max_user_id": None,
    }


def _user_row(user_id: int, role: str = "worker",
              max_user_id: int | None = None) -> dict:
    return {
        "id": user_id, "phone": "+79005556677", "full_name": "Target",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": 1,
        "updated_at": datetime.now(), "updated_by": 1,
        "state": UserState.ACTIVE.value, "role": role,
        "max_user_id": max_user_id,
    }


def _invite_row() -> dict:
    return {
        "token": "tok-abc123", "user_id": 42,
        "expires_at": datetime.now() + timedelta(hours=24),
        "used_at": None, "created_at": datetime.now(),
    }


class TestPrivilegeGuard(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        app = make_app(self.db_mock)
        # Подменяем реальный AuthApiSessionClient мок-объектом, чтобы
        # best-effort revoke_user_sessions(...) после смены пароля/2FA не
        # уходил в сеть (http://auth_api:8003) при изолированном прогоне
        # unit-тестов.
        self.session_client = MagicMock()
        self.session_client.revoke_user_sessions = AsyncMock(return_value=1)
        app.settings["auth_session_client"] = self.session_client
        return app

    def setUp(self):
        super().setUp()
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.fetch_one_strict.reset_mock()
        self.db_mock.execute.reset_mock()

    # ── PATCH: эскалация ────────────────────────────────────────────────
    def test_operator_self_promote_to_admin_forbidden(self):
        """operator PATCH сам себя role=administrator → 403."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),       # prepare auth (caller=operator id=5)
            _user_row(5, role="operator"),  # target = сам caller
        ]
        resp = self.fetch(
            "/api/v1/user/5", method="PATCH",
            body=json.dumps({"role": "administrator"}),
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.execute.assert_not_awaited()

    def test_operator_set_admin_role_on_other_forbidden(self):
        """operator PATCH чужого user role=administrator → 403."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(42, role="worker"),
        ]
        resp = self.fetch(
            "/api/v1/user/42", method="PATCH",
            body=json.dumps({"role": "administrator"}),
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.execute.assert_not_awaited()

    def test_operator_reset_admin_password_forbidden(self):
        """operator PATCH администратору password=... → 403."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(1, role="administrator"),
        ]
        resp = self.fetch(
            "/api/v1/user/1", method="PATCH",
            body=json.dumps({"password": "newpass123"}),
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.execute.assert_not_awaited()

    def test_operator_modify_other_operator_forbidden(self):
        """operator PATCH другого operator'а (password) → 403 (правило 2)."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(7, role="operator"),
        ]
        resp = self.fetch(
            "/api/v1/user/7", method="PATCH",
            body=json.dumps({"password": "newpass123"}),
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.execute.assert_not_awaited()

    # ── PATCH: легитимные ───────────────────────────────────────────────
    def test_operator_update_worker_phone_ok(self):
        """operator меняет телефон worker'а → 200."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(42, role="worker"),  # target load
            None,                          # get_by_phone (уникальность)
            _user_row(42, role="worker"),  # get_by_id после update
        ]
        resp = self.fetch(
            "/api/v1/user/42", method="PATCH",
            body=json.dumps({"phone": "+79009998877"}),
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 200)

    def test_operator_set_worker_role_worker_ok(self):
        """operator подтверждает role=worker worker'у → 200."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(42, role="worker"),
            _user_row(42, role="worker"),  # get_by_id после update
        ]
        resp = self.fetch(
            "/api/v1/user/42", method="PATCH",
            body=json.dumps({"role": "worker"}),
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 200)

    def test_administrator_promote_worker_to_operator_ok(self):
        """administrator повышает worker'а до operator → 200."""
        self.db_mock.fetch_one.side_effect = [
            _admin_auth_row(1),
            _user_row(42, role="worker"),
            _user_row(42, role="operator"),  # get_by_id после update
        ]
        resp = self.fetch(
            "/api/v1/user/42", method="PATCH",
            body=json.dumps({"role": "operator"}),
            headers={"X-Remote-User": "1", "X-Remote-Role": "administrator"},
        )
        self.assertEqual(resp.code, 200)

    # ── 2FA toggles ─────────────────────────────────────────────────────
    def test_operator_disable_admin_2fa_forbidden(self):
        """operator POST /user/{admin}/disable-2fa → 403."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(1, role="administrator"),
        ]
        resp = self.fetch(
            "/api/v1/user/1/disable-2fa", method="POST", body="",
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.execute.assert_not_awaited()

    def test_operator_enable_admin_2fa_forbidden(self):
        """operator POST /user/{admin}/enable-2fa → 403."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(1, role="administrator"),
        ]
        resp = self.fetch(
            "/api/v1/user/1/enable-2fa", method="POST", body="",
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 403)
        self.db_mock.execute.assert_not_awaited()

    def test_operator_disable_worker_2fa_ok(self):
        """operator снимает 2FA с worker'а → 204."""
        self.db_mock.fetch_one.side_effect = [
            _operator_auth_row(5),
            _user_row(42, role="worker"),
            {"password_hash": "h", "require_2fa": True},  # get_auth_record
        ]
        resp = self.fetch(
            "/api/v1/user/42/disable-2fa", method="POST", body="",
            headers={"X-Remote-User": "5", "X-Remote-Role": "operator"},
        )
        self.assertEqual(resp.code, 204)

    def test_administrator_disable_admin_2fa_ok(self):
        """administrator снимает 2FA с другого администратора → 204."""
        self.db_mock.fetch_one.side_effect = [
            _admin_auth_row(1),
            _user_row(2, role="administrator"),
            {"password_hash": "h", "require_2fa": True},
        ]
        resp = self.fetch(
            "/api/v1/user/2/disable-2fa", method="POST", body="",
            headers={"X-Remote-User": "1", "X-Remote-Role": "administrator"},
        )
        self.assertEqual(resp.code, 204)


if __name__ == "__main__":
    unittest.main()
