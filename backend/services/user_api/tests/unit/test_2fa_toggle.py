"""Unit-тесты enable-2fa / disable-2fa (handler-level, db мокается)."""
from datetime import datetime, timedelta
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.user import UserState


def _auth_row() -> dict:
    """Строка, которую возвращает prepare()-овский get_by_id для X-Remote-User."""
    return {
        "id": 1, "phone": "+79123456789", "full_name": "Admin",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": None,
        "updated_at": datetime.now(), "updated_by": None,
        "state": UserState.ACTIVE.value, "role": "administrator",
        "max_user_id": None,
    }


def _user_row(user_id: int = 42, max_user_id: int | None = None) -> dict:
    return {
        "id": user_id, "phone": "+79005556677", "full_name": "Target",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": 1,
        "updated_at": datetime.now(), "updated_by": 1,
        "state": UserState.ACTIVE.value, "role": "worker",
        "max_user_id": max_user_id,
    }


def _invite_row() -> dict:
    return {
        "token": "tok-abc123", "user_id": 42,
        "expires_at": datetime.now() + timedelta(hours=24),
        "used_at": None, "created_at": datetime.now(),
    }


class TestEnableDisable2FA(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        app = make_app(self.db_mock)
        # Подменяем реальный AuthApiSessionClient мок-объектом, чтобы
        # best-effort revoke_user_sessions(...) после смены 2FA не уходил
        # в сеть (http://auth_api:8003) при изолированном прогоне unit-тестов.
        self.session_client = MagicMock()
        self.session_client.revoke_user_sessions = AsyncMock(return_value=1)
        app.settings["auth_session_client"] = self.session_client
        return app

    def setUp(self):
        super().setUp()
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.fetch_one_strict.reset_mock()
        self.db_mock.execute.reset_mock()

    # ── enable ────────────────────────────────────────────────────────────
    def test_enable_bound_returns_204(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),                                  # prepare auth
            _user_row(42, max_user_id=99999),             # get_by_id target (bound)
            {"password_hash": "h", "require_2fa": False}, # get_auth_record
        ]
        resp = self.fetch("/api/v1/user/42/enable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 204)
        args, _ = self.db_mock.execute.await_args
        self.assertEqual(args[1], (True, 42))

    def test_enable_unbound_returns_200_with_invite(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _user_row(42, max_user_id=None),              # unbound
            {"password_hash": "h", "require_2fa": False},
        ]
        self.db_mock.fetch_one_strict.return_value = _invite_row()
        resp = self.fetch("/api/v1/user/42/enable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["require_2fa"], True)
        self.assertEqual(body["max_bound"], False)
        self.assertEqual(body["max_invite_token"], "tok-abc123")
        args, _ = self.db_mock.execute.await_args
        self.assertEqual(args[1], (True, 42))

    def test_enable_no_password_returns_409(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _user_row(42),
            None,                                         # get_auth_record → нет пароля
        ]
        resp = self.fetch("/api/v1/user/42/enable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 409)
        self.db_mock.execute.assert_not_awaited()

    def test_enable_missing_user_returns_404(self):
        self.db_mock.fetch_one.side_effect = [_auth_row(), None]
        resp = self.fetch("/api/v1/user/777/enable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 404)

    def test_enable_bad_id_returns_400(self):
        self.db_mock.fetch_one.side_effect = [_auth_row()]
        resp = self.fetch("/api/v1/user/abc/enable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 400)

    # ── disable ───────────────────────────────────────────────────────────
    def test_disable_clears_flag_returns_204(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _user_row(42),
            {"password_hash": "h", "require_2fa": True},
        ]
        resp = self.fetch("/api/v1/user/42/disable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 204)
        args, _ = self.db_mock.execute.await_args
        self.assertEqual(args[1], (False, 42))

    def test_disable_idempotent_no_auth_row_returns_204(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _user_row(42),
            None,                                         # нет user_auth — нечего выключать
        ]
        resp = self.fetch("/api/v1/user/42/disable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 204)
        self.db_mock.execute.assert_not_awaited()

    def test_disable_bad_id_returns_400(self):
        self.db_mock.fetch_one.side_effect = [_auth_row()]
        resp = self.fetch("/api/v1/user/abc/disable-2fa", method="POST",
                          body="", headers={"X-Remote-User": "1"})
        self.assertEqual(resp.code, 400)
