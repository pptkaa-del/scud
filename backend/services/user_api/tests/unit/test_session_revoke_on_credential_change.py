"""Unit-тесты SEC-14: смена учётных данных отзывает сессии (best-effort).

Проверяем, что user_api зовёт AuthApiSessionClient.revoke_user_sessions с
правильным user_id после смены пароля и переключения 2FA, и что отказ
auth_api НЕ проваливает основную операцию (handler возвращает успех,
логирует WARNING).
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

from tornado.httpclient import HTTPError
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
        "max_user_id": None,
    }


def _target_row(user_id: int = 2, max_user_id: int | None = None) -> dict:
    return {
        "id": user_id, "phone": "+79005556677", "full_name": "Target",
        "contractor_id": None, "contractor_name": None,
        "created_at": datetime.now(), "created_by": 1,
        "updated_at": datetime.now(), "updated_by": 1,
        "state": UserState.ACTIVE.value, "role": "worker",
        "max_user_id": max_user_id,
    }


class TestSessionRevokeOnCredentialChange(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        app = make_app(self.db_mock)
        # Подменяем реальный клиент мок-объектом.
        self.session_client = MagicMock()
        self.session_client.revoke_user_sessions = AsyncMock(return_value=1)
        app.settings["auth_session_client"] = self.session_client
        return app

    def setUp(self):
        super().setUp()
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.execute.reset_mock()
        self.session_client.revoke_user_sessions.reset_mock()
        self.session_client.revoke_user_sessions.side_effect = None
        self.session_client.revoke_user_sessions.return_value = 1

    # ── password change ──────────────────────────────────────────────────
    def test_password_change_revokes_sessions(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _target_row(),
            _target_row(),  # updated
        ]
        resp = self.fetch(
            "/api/v1/user/2", method="PATCH",
            body='{"password": "newpassword123"}',
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(resp.code, 200)
        self.session_client.revoke_user_sessions.assert_awaited_once_with(2)

    def test_password_change_succeeds_when_revoke_fails(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _target_row(),
            _target_row(),
        ]
        self.session_client.revoke_user_sessions.side_effect = HTTPError(503, "down")
        resp = self.fetch(
            "/api/v1/user/2", method="PATCH",
            body='{"password": "newpassword123"}',
            headers={"X-Remote-User": "1"},
        )
        # Смена пароля прошла, несмотря на падение auth_api.
        self.assertEqual(resp.code, 200)
        self.session_client.revoke_user_sessions.assert_awaited_once_with(2)

    def test_patch_without_password_does_not_revoke(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _target_row(),
            _target_row(),
        ]
        resp = self.fetch(
            "/api/v1/user/2", method="PATCH",
            body='{"full_name": "New Name"}',
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(resp.code, 200)
        self.session_client.revoke_user_sessions.assert_not_awaited()

    # ── 2FA enable ───────────────────────────────────────────────────────
    def test_enable_2fa_revokes_sessions(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _target_row(2, max_user_id=99999),
            {"password_hash": "h", "require_2fa": False},
        ]
        resp = self.fetch(
            "/api/v1/user/2/enable-2fa", method="POST", body="",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(resp.code, 204)
        self.session_client.revoke_user_sessions.assert_awaited_once_with(2)

    # ── 2FA disable ──────────────────────────────────────────────────────
    def test_disable_2fa_revokes_sessions(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _target_row(2),
            {"password_hash": "h", "require_2fa": True},
        ]
        resp = self.fetch(
            "/api/v1/user/2/disable-2fa", method="POST", body="",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(resp.code, 204)
        self.session_client.revoke_user_sessions.assert_awaited_once_with(2)

    def test_disable_2fa_no_auth_record_does_not_revoke(self):
        self.db_mock.fetch_one.side_effect = [
            _auth_row(),
            _target_row(2),
            None,  # нет user_auth — настройка не меняется
        ]
        resp = self.fetch(
            "/api/v1/user/2/disable-2fa", method="POST", body="",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(resp.code, 204)
        self.session_client.revoke_user_sessions.assert_not_awaited()
