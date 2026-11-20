"""Unit-тесты для API пользователей"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

import psycopg2.errors
from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.user import UserState


class TestUserApi(AsyncHTTPTestCase):
    def get_app(self):
        # Создаем мок БД напрямую (AsyncHTTPTestCase не совместим с pytest fixtures)
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()
        app = make_app(self.db_mock)
        # Подменяем реальный AuthApiSessionClient мок-объектом, чтобы
        # best-effort revoke_user_sessions(...) после смены пароля не уходил
        # в сеть (http://auth_api:8003) при изолированном прогоне unit-тестов.
        self.session_client = MagicMock()
        self.session_client.revoke_user_sessions = AsyncMock(return_value=1)
        app.settings["auth_session_client"] = self.session_client
        return app

    def setUp(self):
        super().setUp()
        # Reset mocks before each test
        self.db_mock.fetch_one.reset_mock()
        self.db_mock.fetch_one_strict.reset_mock()
        self.db_mock.fetch_all.reset_mock()
        self.db_mock.execute.reset_mock()

        self.auth_user_row = {
            "id": 1, "phone": "+79123456789", "full_name": "Admin", "contractor_id": None,
            "contractor_name": None,
            "created_at": datetime.now(), "created_by": None, "updated_at": datetime.now(), "updated_by": None,
            "state": UserState.ACTIVE.value, "role": "administrator"
        }

    def test_unauthorized_missing_header(self):
        """Тест: отсутствует заголовок X-Remote-User"""
        response = self.fetch("/api/v1/user/1", method="GET")
        self.assertEqual(response.code, 401)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Unauthorized")
        self.assertEqual(data["status"], 401)
        self.assertIn("Missing X-Remote-User", data["detail"])

    def test_unauthorized_invalid_header(self):
        """Тест: невалидный заголовок X-Remote-User"""
        response = self.fetch("/api/v1/user/1", method="GET", headers={"X-Remote-User": "abc"})
        self.assertEqual(response.code, 401)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Unauthorized")
        self.assertEqual(data["status"], 401)
        self.assertIn("Invalid X-Remote-User", data["detail"])

    def test_unauthorized_user_not_found(self):
        """Тест: пользователь не найден при авторизации"""
        self.db_mock.fetch_one.return_value = None
        response = self.fetch("/api/v1/user/1", method="GET", headers={"X-Remote-User": "999"})
        self.assertEqual(response.code, 401)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Unauthorized")
        self.assertEqual(data["status"], 401)
        self.assertIn("User not found", data["detail"])

    def test_get_user_success(self):
        """Тест: успешное получение пользователя по ID"""
        target_user = {
            "id": 2,
            "phone": "+79005556677",
            "full_name": "Test User",
            "contractor_id": 10,
            "contractor_name": "ООО Тест",
            "created_at": datetime.now(),
            "created_by": 1,
            "updated_at": datetime.now(),
            "updated_by": 1,
            "state": UserState.ACTIVE.value,
            "role": "worker"
        }

        # side_effect для возврата текущего пользователя (auth) и целевого пользователя
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # Первый вызов - получение текущего пользователя
            target_user  # Второй вызов - получение целевого пользователя
        ]

        response = self.fetch("/api/v1/user/2", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], 2)
        self.assertEqual(data["full_name"], "Test User")
        self.assertEqual(data["contractor_name"], "ООО Тест")

    def test_get_user_not_found(self):
        """Тест: пользователь не найден"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # current user found
            None  # target user NOT found
        ]
        response = self.fetch("/api/v1/user/2", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("User", data["detail"])
        self.assertIn("not found", data["detail"])

    def test_create_user_success(self):
        """Тест: успешное создание пользователя"""
        from datetime import timedelta
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth user
            None,                # check if exists by phone
            {                    # get_by_id returns full user (после INSERT)
                "id": 3, "phone": "+79110000000", "full_name": "New User", "contractor_id": None,
                "contractor_name": None,
                "created_at": datetime.now(), "created_by": 1, "updated_at": datetime.now(), "updated_by": 1,
                "state": UserState.ACTIVE, "role": "worker"
            }
        ]
        self.db_mock.fetch_one_strict.side_effect = [
            {"id": 3},  # INSERT users RETURNING id
            {           # INSERT max_invite_tokens RETURNING row
                "token": "test-invite-token",
                "user_id": 3,
                "expires_at": datetime.now() + timedelta(hours=24),
                "used_at": None,
                "created_at": datetime.now(),
            },
        ]

        body = {"phone": "+79110000000", "full_name": "New User"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["id"], 3)
        self.assertEqual(data["phone"], "+79110000000")
        self.assertIsNone(data["contractor_name"])
        # Новый функционал: создание пользователя возвращает invite-токен для Max.
        self.assertEqual(data["max_invite_token"], "test-invite-token")
        self.assertIn("max_invite_expires_at", data)

    def test_create_user_conflict(self):
        """Тест: конфликт при создании пользователя с существующим телефоном"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth user
            {  # check if exists by phone -> returns existing user
                "id": 2, "phone": "+79005556677", "full_name": "Existing", "contractor_id": None,
                "contractor_name": None,
                "created_at": datetime.now(), "created_by": 1, "updated_at": datetime.now(), "updated_by": 1,
                "state": UserState.ACTIVE, "role": "worker"
            }
        ]

        body = {"phone": "+79005556677", "full_name": "New"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 409)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Conflict")
        self.assertEqual(data["status"], 409)
        self.assertIn("phone", data["detail"])
        self.assertIn("already exists", data["detail"])

    def test_create_user_duplicate_phone_race_returns_409(self):
        """COR-34: TOCTOU-гонка — get_by_phone прошёл, но INSERT упёрся в
        unique-constraint на users.phone → 409, а не 500.

        Раньше unique-violation от INSERT поднималась как raw psycopg2-ошибка
        и долетала до write_error → 500. Теперь handler ловит её и отдаёт 409.
        """
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # auth user
            None,                # get_by_phone -> свободно (TOCTOU прошёл)
        ]
        # INSERT users RETURNING id поднимает unique-violation (гонка).
        self.db_mock.fetch_one_strict.side_effect = psycopg2.errors.UniqueViolation(
            'duplicate key value violates unique constraint "users_phone_key"'
        )

        body = {"phone": "+79110000000", "full_name": "Racy User"}
        response = self.fetch("/api/v1/users", method="POST",
                              body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 409)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 409)
        self.assertIn("already exists", data["detail"])

    def test_create_user_unknown_field_returns_400(self):
        """VAL-03: неизвестный ключ в теле запроса → 400, а не молча игнор.

        Раньше dacite молча отбрасывал лишние ключи: опечатка в имени поля
        тихо подставляла default. Теперь get_json_body_as(strict=True) отвергает
        неизвестные ключи 400.
        """
        self.db_mock.fetch_one.return_value = self.auth_user_row  # auth user

        body = {
            "phone": "+79110000000",
            "full_name": "New User",
            "max_retires": 5,  # опечатка/неизвестное поле
        }
        response = self.fetch("/api/v1/users", method="POST",
                              body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 400)

    def test_delete_user_success(self):
        """Тест: успешное удаление пользователя"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        response = self.fetch("/api/v1/user/2", method="DELETE", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)
        self.db_mock.execute.assert_called_once()

    def test_get_users_list_success(self):
        """Тест: успешное получение списка пользователей"""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 2}]
        self.db_mock.fetch_all.return_value = [
            {
                "id": 2, "phone": "+79001112233", "full_name": "User 1", "contractor_id": None,
                "contractor_name": None,
                "created_at": datetime.now(), "created_by": 1, "updated_at": datetime.now(),
                "updated_by": 1, "state": UserState.ACTIVE, "role": "worker"
            },
            {
                "id": 3, "phone": "+79004445566", "full_name": "User 2", "contractor_id": 10,
                "contractor_name": "ООО Подрядчик",
                "created_at": datetime.now(), "created_by": 1, "updated_at": datetime.now(),
                "updated_by": 1, "state": UserState.ACTIVE, "role": "worker"
            }
        ]

        response = self.fetch("/api/v1/users", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 2)
        self.assertEqual(len(data["items"]), 2)
        self.assertEqual(data["items"][0]["id"], 2)
        self.assertIsNone(data["items"][0]["contractor_name"])
        self.assertEqual(data["items"][1]["contractor_name"], "ООО Подрядчик")

    def test_get_users_list_with_filters(self):
        """Тест: получение списка пользователей с фильтрами"""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch("/api/v1/users?state=active&contractor_id=10",
                            method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 0)
        self.assertEqual(len(data["items"]), 0)

    def test_get_users_with_search_filter(self):
        """`?q=` применяет ILIKE-фильтр (с ESCAPE) по full_name в get_all и count."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch("/api/v1/users?q=Иванов",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)

        get_all_args = self.db_mock.fetch_all.call_args
        self.assertIn("u.full_name ILIKE %s ESCAPE '\\'", get_all_args[0][0])
        self.assertIn("%Иванов%", get_all_args[0][1])

        count_args = self.db_mock.fetch_one.call_args  # последний fetch_one — count()
        self.assertIn("u.full_name ILIKE %s ESCAPE '\\'", count_args[0][0])
        self.assertIn("%Иванов%", count_args[0][1])

    def test_get_users_empty_search_treated_as_no_filter(self):
        """`?q=` (пустая строка) → без ILIKE в SQL."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch("/api/v1/users?q=",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.assertNotIn("ILIKE", self.db_mock.fetch_all.call_args[0][0])
        self.assertNotIn("ILIKE", self.db_mock.fetch_one.call_args[0][0])

    def test_get_users_search_escapes_like_metachars(self):
        """`?q=50%` экранирует LIKE-метасимвол: в параметрах — `%50\\%%`."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch("/api/v1/users?q=50%25",  # %25 == '%'
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        self.assertIn("%50\\%%", self.db_mock.fetch_all.call_args[0][1])

    def test_get_users_search_too_long_returns_400(self):
        """`?q=` длиннее 255 символов → 400."""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row]

        long_q = "a" * 256
        response = self.fetch(f"/api/v1/users?q={long_q}",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 400)

    def test_get_users_invalid_state_empty(self):
        """Тест: пустой state возвращает 400"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        response = self.fetch("/api/v1/users?state=",
                             method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertIn("Invalid state", data["detail"])

    def test_get_users_invalid_state_typo(self):
        """Тест: неправильное значение state (deactivate вместо deactivated)"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        response = self.fetch("/api/v1/users?state=deactivate",
                             method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertIn("Invalid state", data["detail"])

    def test_get_users_valid_state_deactivated(self):
        """Тест: корректное значение state=deactivated проходит валидацию"""
        self.db_mock.fetch_one.side_effect = [self.auth_user_row, {"count": 0}]
        self.db_mock.fetch_all.return_value = []

        response = self.fetch("/api/v1/users?state=deactivated",
                             method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)

    def test_create_user_invalid_phone_no_plus(self):
        """Тест: телефон без знака +"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "79001234567", "full_name": "Test User"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("phone", data["detail"])
        self.assertIn("+79999999999", data["detail"])

    def test_create_user_invalid_phone_wrong_code(self):
        """Тест: неправильный код страны (не +7)"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "+89001234567", "full_name": "Test User"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("phone", data["detail"])

    def test_create_user_invalid_phone_short(self):
        """Тест: слишком короткий номер (9 цифр вместо 10)"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "+7900123456", "full_name": "Test User"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("phone", data["detail"])

    def test_create_user_invalid_phone_long(self):
        """Тест: слишком длинный номер (11 цифр вместо 10)"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "+790012345678", "full_name": "Test User"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("phone", data["detail"])

    def test_create_user_invalid_phone_letters(self):
        """Тест: буквы в номере телефона"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "+7900abcd567", "full_name": "Test User"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("phone", data["detail"])

    def test_create_user_invalid_phone_empty(self):
        """Тест: пустая строка вместо телефона"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "", "full_name": "Test User"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body), headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Validation Error")
        self.assertEqual(data["status"], 400)
        self.assertIn("phone", data["detail"])

    def test_create_user_with_password_stores_hash(self):
        """Тест: создание с паролем вызывает execute для user_auth с bcrypt-хешем"""
        from datetime import datetime, timedelta
        created_user = {
            "id": 3, "phone": "+79110000000", "full_name": "New User", "contractor_id": None,
            "contractor_name": None,
            "created_at": datetime.now(), "created_by": 1, "updated_at": datetime.now(), "updated_by": 1,
            "state": UserState.ACTIVE, "role": "worker"
        }
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # prepare() — текущий пользователь
            None,                # get_by_phone — не существует
            created_user,        # get_by_id — полный объект (после INSERT)
        ]
        self.db_mock.fetch_one_strict.side_effect = [
            {"id": 3},
            {"token": "tk", "user_id": 3, "expires_at": datetime.now() + timedelta(hours=24),
             "used_at": None, "created_at": datetime.now()},
        ]

        body = {"phone": "+79110000000", "full_name": "New User", "password": "secret123"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 201)
        # execute вызван ровно один раз — для INSERT INTO user_auth
        self.db_mock.execute.assert_called_once()
        call_args = self.db_mock.execute.call_args
        # Второй аргумент — (user_id, password_hash, require_2fa)
        params = call_args[0][1]
        self.assertEqual(params[0], 3)               # user_id
        self.assertTrue(params[1].startswith("$2b$"))  # bcrypt hash
        self.assertTrue(params[2])                     # require_2fa=True по умолчанию для новых юзеров

    def test_create_user_without_password_skips_hash(self):
        """Тест: создание без пароля не вызывает execute (user_auth не создаётся)"""
        from datetime import datetime, timedelta
        created_user = {
            "id": 4, "phone": "+79120000000", "full_name": "No Pass", "contractor_id": None,
            "contractor_name": None,
            "created_at": datetime.now(), "created_by": 1, "updated_at": datetime.now(), "updated_by": 1,
            "state": UserState.ACTIVE, "role": "worker"
        }
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            None,
            created_user,  # get_by_id после INSERT
        ]
        self.db_mock.fetch_one_strict.side_effect = [
            {"id": 4},
            {"token": "tk2", "user_id": 4, "expires_at": datetime.now() + timedelta(hours=24),
             "used_at": None, "created_at": datetime.now()},
        ]

        body = {"phone": "+79120000000", "full_name": "No Pass"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 201)
        self.db_mock.execute.assert_not_called()

    def test_create_user_with_short_password_returns_400(self):
        """Тест: пароль короче 8 символов → 400"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "+79130000000", "full_name": "Test", "password": "short"}
        response = self.fetch("/api/v1/users", method="POST", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 400)
        self.db_mock.execute.assert_not_called()

    # ------------------------------------------------------------------
    # PATCH /api/v1/user/{id}
    # ------------------------------------------------------------------

    def _target_user_row(self, user_id: int = 2, phone: str = "+79005556677",
                         full_name: str = "Original Name", role: str = "worker") -> dict:
        return {
            "id": user_id, "phone": phone, "full_name": full_name,
            "contractor_id": None, "contractor_name": None,
            "created_at": datetime.now(), "created_by": 1,
            "updated_at": datetime.now(), "updated_by": 1,
            "state": UserState.ACTIVE.value, "role": role,
        }

    def test_patch_user_update_full_name(self):
        """Тест: обновление имени пользователя"""
        updated = self._target_user_row(full_name="New Name")
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # prepare() auth
            self._target_user_row(),  # get_by_id — проверка существования
            None,                # get_by_phone — не вызывается (телефон не меняется)
            updated,             # get_by_id после UPDATE
        ]
        # get_by_phone не вызывается, поэтому side_effect нужно скорректировать
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            self._target_user_row(),
            updated,
        ]

        body = {"full_name": "New Name"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["full_name"], "New Name")
        self.db_mock.execute.assert_called_once()

    def test_patch_user_update_phone_no_conflict(self):
        """Тест: обновление телефона — новый телефон свободен"""
        new_phone = "+79007778899"
        updated = self._target_user_row(phone=new_phone)
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,        # prepare() auth
            self._target_user_row(),   # get_by_id — цель существует
            None,                      # get_by_phone — новый телефон свободен
            updated,                   # get_by_id после UPDATE
        ]

        body = {"phone": new_phone}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["phone"], new_phone)

    def test_patch_user_phone_conflict(self):
        """Тест: обновление телефона — телефон занят другим пользователем → 409"""
        existing = self._target_user_row(user_id=99, phone="+79007778899")
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,       # prepare() auth
            self._target_user_row(),  # get_by_id — цель существует
            existing,                 # get_by_phone — занят
        ]

        body = {"phone": "+79007778899"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 409)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Conflict")

    def test_patch_user_not_found(self):
        """Тест: пользователь для обновления не найден → 404"""
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,  # prepare() auth
            None,                # get_by_id — не найден
        ]

        body = {"full_name": "New Name"}
        response = self.fetch("/api/v1/user/999", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")

    def test_patch_user_empty_body(self):
        """Тест: пустое тело запроса → 400"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 400)

    def test_patch_user_invalid_phone(self):
        """Тест: невалидный формат телефона → 400"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"phone": "not-a-phone"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 400)
        self.assertIn("phone", data["detail"])

    def test_patch_user_update_role(self):
        """Тест: обновление роли пользователя"""
        updated = self._target_user_row(role="operator")
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            self._target_user_row(),
            updated,
        ]

        body = {"role": "operator"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["role"], "operator")

    def test_patch_user_update_password(self):
        """Тест: обновление пароля — execute вызывается для user_auth"""
        updated = self._target_user_row()
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            self._target_user_row(),
            updated,
        ]

        body = {"password": "newpassword123"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 200)
        # execute: 1 раз для UPDATE users (пустой — только пароль без других полей)
        # + 1 раз для user_auth INSERT/UPDATE
        self.assertEqual(self.db_mock.execute.call_count, 1)
        call_args = self.db_mock.execute.call_args
        params = call_args[0][1]
        self.assertTrue(params[1].startswith("$2b$"))  # bcrypt hash
        # Смена пароля НЕ трогает require_2fa: только (user_id, password_hash).
        self.assertEqual(len(params), 2)

    def test_patch_user_short_password(self):
        """Тест: пароль короче 8 символов при обновлении → 400"""
        self.db_mock.fetch_one.return_value = self.auth_user_row

        body = {"password": "short"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 400)

    def test_patch_user_same_phone_skips_uniqueness_check(self):
        """Тест: телефон совпадает с текущим — проверка уникальности не вызывается"""
        current_phone = "+79005556677"
        updated = self._target_user_row(phone=current_phone, full_name="New Name")
        self.db_mock.fetch_one.side_effect = [
            self.auth_user_row,
            self._target_user_row(phone=current_phone),  # target с тем же телефоном
            updated,  # get_by_id после UPDATE
        ]

        body = {"phone": current_phone, "full_name": "New Name"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body),
                              headers={"X-Remote-User": "1"})

        # get_by_phone не должен был вызываться (телефон не изменился)
        self.assertEqual(response.code, 200)
        # fetch_one: auth + get_by_id(target) + get_by_id(after update) = 3
        self.assertEqual(self.db_mock.fetch_one.call_count, 3)

    def test_patch_user_unauthorized(self):
        """Тест: отсутствует заголовок X-Remote-User → 401"""
        body = {"full_name": "New Name"}
        response = self.fetch("/api/v1/user/2", method="PATCH", body=json.dumps(body))
        self.assertEqual(response.code, 401)
