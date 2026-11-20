"""Unit-тесты: статус включения 2FA (require_2fa) в ответах чтения пользователей.

GET /users и GET /user/{id} должны отдавать флаг require_2fa каждого
пользователя. Флаг хранится в отдельной таблице user_auth (LEFT JOIN),
для пользователя без записи user_auth (пароль не задан) он равен False.
"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from dacite import Config, from_dict
from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.user import User, UserRole, UserState

_CFG = Config(cast=[UserState, UserRole])


def _row(user_id: int, role: str = "worker", **extra: object) -> dict:
    base = {
        "id": user_id,
        "phone": f"+7900000{user_id:04d}",
        "full_name": f"User {user_id}",
        "contractor_id": None,
        "contractor_name": None,
        "created_at": datetime.now(),
        "created_by": 1,
        "updated_at": datetime.now(),
        "updated_by": 1,
        "state": UserState.ACTIVE.value,
        "role": role,
    }
    base.update(extra)
    return base


class TestUser2FAStatusModel(AsyncHTTPTestCase):
    """Модель-уровень: значение require_2fa и null-safety по умолчанию."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        return make_app(self.db_mock)

    def test_row_with_require_2fa_true_maps_to_field(self):
        user = from_dict(User, _row(2, require_2fa=True), config=_CFG)
        self.assertIs(user.require_2fa, True)

    def test_row_with_require_2fa_false_maps_to_field(self):
        user = from_dict(User, _row(2, require_2fa=False), config=_CFG)
        self.assertIs(user.require_2fa, False)

    def test_row_without_require_2fa_defaults_false(self):
        # Пользователь без записи user_auth: ключ отсутствует → default False.
        user = from_dict(User, _row(2), config=_CFG)
        self.assertIs(user.require_2fa, False)


class TestUser2FAStatusApi(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        return make_app(self.db_mock)

    def setUp(self):
        super().setUp()
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.fetch_one_strict.reset_mock()
        self.db_mock.fetch_all.reset_mock()
        self.db_mock.execute.reset_mock()
        self.auth_user_row = _row(1, role="administrator")

    # ------------------------------------------------------------------
    # GET /user/{id}
    # ------------------------------------------------------------------

    def test_get_user_includes_require_2fa_true(self):
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            _row(2, require_2fa=True),
        ]
        response = self.fetch("/api/v1/user/2", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertIn("require_2fa", data)
        self.assertIs(data["require_2fa"], True)

    def test_get_user_includes_require_2fa_false(self):
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            _row(2, require_2fa=False),
        ]
        response = self.fetch("/api/v1/user/2", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertIs(data["require_2fa"], False)

    def test_get_by_id_query_left_joins_user_auth(self):
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            _row(2, require_2fa=True),
        ]
        self.fetch("/api/v1/user/2", method="GET", headers={"X-Remote-User": "1"})
        # Последний fetch_one — get_by_id(target).
        sql = self.db_mock.fetch_one.call_args[0][0]
        self.assertIn("user_auth", sql)
        self.assertIn("require_2fa", sql)

    # ------------------------------------------------------------------
    # GET /users
    # ------------------------------------------------------------------

    def test_get_users_list_items_include_require_2fa(self):
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 2}]
        self.db_mock.fetch_all.return_value = [
            _row(2, require_2fa=True),
            _row(3, require_2fa=False),
        ]
        response = self.fetch("/api/v1/users", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertIs(data["items"][0]["require_2fa"], True)
        self.assertIs(data["items"][1]["require_2fa"], False)

    def test_get_all_query_left_joins_user_auth(self):
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []
        self.fetch("/api/v1/users", method="GET", headers={"X-Remote-User": "1"})
        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertIn("user_auth", sql)
        self.assertIn("require_2fa", sql)

    # ------------------------------------------------------------------
    # POST /users — ответ на создание должен отражать реальный 2FA-статус,
    # а не состояние ДО записи user_auth (иначе create-ответ противоречит
    # последующему GET и примеру в OpenAPI).
    # ------------------------------------------------------------------

    def _create_flow_mocks(self, created_row: dict) -> None:
        from datetime import timedelta
        # get_by_id (created) отдаёт require_2fa=False — user_auth ещё не создан
        # в момент сериализации внутри repo.create().
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # prepare() auth
            None,                # get_by_phone — свободно
            created_row,         # get_by_id после INSERT
        ]
        self.db_mock.fetch_one_strict.side_effect = [
            {"id": created_row["id"]},  # INSERT users RETURNING id
            {  # INSERT max_invite_tokens RETURNING row
                "token": "tk", "user_id": created_row["id"],
                "expires_at": datetime.now() + timedelta(hours=24),
                "used_at": None, "created_at": datetime.now(),
            },
        ]

    def test_create_user_with_password_response_require_2fa_true(self):
        self._create_flow_mocks(_row(5, require_2fa=False))
        body = {"phone": "+79110000005", "full_name": "With Pass", "password": "secret123"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        # Новый пользователь с паролем получает 2FA по умолчанию (require_2fa=True),
        # и ответ на создание обязан это отражать.
        self.assertIs(data["require_2fa"], True)

    def test_create_user_without_password_response_require_2fa_false(self):
        self._create_flow_mocks(_row(6, require_2fa=False))
        body = {"phone": "+79110000006", "full_name": "No Pass"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        # Без пароля строки user_auth нет → require_2fa=False.
        self.assertIs(data["require_2fa"], False)
