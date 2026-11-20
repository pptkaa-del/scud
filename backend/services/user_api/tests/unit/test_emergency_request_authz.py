"""Unit-тесты авторизации заявок на экстренное открытие шкафов (SEC-08/09).

Покрывают два класса дефектов:

* SEC-08 — worker может создать заявку на ЛЮБОЙ шкаф без активного Допуска.
  Handler обязан проверить Допуск ДО ``repo.create`` и вернуть 403, если его
  нет. Operator/administrator не ограничены Допуском.
* SEC-09 — worker мог перечислять и читать ЧУЖИЕ заявки (IDOR). Список должен
  быть отскоупен на ``user_id`` вызывающего, а detail чужой заявки — отдавать
  404. Operator/administrator видят все заявки.
"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.models.emergency_request import EmergencyRequestStatus
from app.user_data.db.models.user import UserState

LOCKER_GRANTED = "550e8400-e29b-41d4-a716-446655440001"
LOCKER_FOREIGN = "550e8400-e29b-41d4-a716-446655440002"

WORKER_ID = 10
OTHER_USER_ID = 20


def _user_row(user_id: int, role: str) -> dict:
    return {
        "id": user_id, "phone": "+79123456789", "full_name": "U", "contractor_id": None,
        "contractor_name": None,
        "created_at": datetime.now(), "created_by": None, "updated_at": datetime.now(),
        "updated_by": None, "state": UserState.ACTIVE.value, "role": role,
    }


def _detail_row(request_id: int, user_id: int, locker_id: str) -> dict:
    return {
        "id": request_id, "user_id": user_id, "user_full_name": "U", "user_phone": "+79123456789",
        "locker_id": locker_id, "status": EmergencyRequestStatus.PENDING.value,
        "created_at": datetime.now(), "closed_at": None, "closed_by": None,
        "closed_by_full_name": None,
    }


class TestEmergencyRequestAuthz(AsyncHTTPTestCase):
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

    # ------------------------------------------------------------------
    # SEC-08: создание заявки — проверка Допуска для worker'а
    # ------------------------------------------------------------------

    def test_worker_without_grant_create_forbidden(self):
        """Worker БЕЗ Допуска на шкаф → POST create → 403 (эксплойт SEC-08)."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(WORKER_ID, "worker"),  # auth
            None,                            # has_active_grant → нет Допуска
        ]

        response = self.fetch(
            "/api/v1/emergency-requests", method="POST",
            body=json.dumps({"locker_id": LOCKER_FOREIGN}),
            headers={"X-Remote-User": str(WORKER_ID)},
        )

        self.assertEqual(response.code, 403)
        # Заявка не должна быть создана.
        self.db_mock.fetch_one_strict.assert_not_called()

    def test_worker_with_grant_create_success(self):
        """Worker С активным Допуском на шкаф → POST create → 201."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(WORKER_ID, "worker"),          # auth
            {"granted": 1},                          # has_active_grant → есть Допуск
            _detail_row(1, WORKER_ID, LOCKER_GRANTED),  # get_by_id details
        ]
        self.db_mock.fetch_one_strict.return_value = {
            "id": 1, "user_id": WORKER_ID, "locker_id": LOCKER_GRANTED,
            "status": EmergencyRequestStatus.PENDING.value,
            "created_at": datetime.now(), "closed_at": None, "closed_by": None,
        }

        response = self.fetch(
            "/api/v1/emergency-requests", method="POST",
            body=json.dumps({"locker_id": LOCKER_GRANTED}),
            headers={"X-Remote-User": str(WORKER_ID)},
        )

        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["locker_id"], LOCKER_GRANTED)
        self.db_mock.fetch_one_strict.assert_called_once()

    def test_operator_create_without_grant_success(self):
        """Operator не ограничен Допуском → POST create → 201 без grant-чека."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(OTHER_USER_ID, "operator"),       # auth
            _detail_row(1, OTHER_USER_ID, LOCKER_FOREIGN),  # get_by_id details
        ]
        self.db_mock.fetch_one_strict.return_value = {
            "id": 1, "user_id": OTHER_USER_ID, "locker_id": LOCKER_FOREIGN,
            "status": EmergencyRequestStatus.PENDING.value,
            "created_at": datetime.now(), "closed_at": None, "closed_by": None,
        }

        response = self.fetch(
            "/api/v1/emergency-requests", method="POST",
            body=json.dumps({"locker_id": LOCKER_FOREIGN}),
            headers={"X-Remote-User": str(OTHER_USER_ID)},
        )

        self.assertEqual(response.code, 201)
        self.db_mock.fetch_one_strict.assert_called_once()

    # ------------------------------------------------------------------
    # SEC-09: список заявок — scoping для worker'а
    # ------------------------------------------------------------------

    def test_worker_list_scoped_to_self(self):
        """Worker GET list → запрос фильтруется по его user_id (эксплойт SEC-09)."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(WORKER_ID, "worker"),  # auth
            {"count": 1},                    # count
        ]
        self.db_mock.fetch_all.return_value = [_detail_row(1, WORKER_ID, LOCKER_GRANTED)]

        response = self.fetch(
            "/api/v1/emergency-requests", method="GET",
            headers={"X-Remote-User": str(WORKER_ID)},
        )

        self.assertEqual(response.code, 200)
        # Запрос списка должен включать фильтр по user_id вызывающего.
        list_call = self.db_mock.fetch_all.call_args
        params = list_call.args[1]
        self.assertIn(WORKER_ID, params)

    def test_operator_list_not_scoped(self):
        """Operator GET list → без фильтра по user_id (видит все заявки)."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(OTHER_USER_ID, "operator"),  # auth
            {"count": 2},                          # count
        ]
        self.db_mock.fetch_all.return_value = [
            _detail_row(1, WORKER_ID, LOCKER_GRANTED),
            _detail_row(2, OTHER_USER_ID, LOCKER_FOREIGN),
        ]

        response = self.fetch(
            "/api/v1/emergency-requests", method="GET",
            headers={"X-Remote-User": str(OTHER_USER_ID)},
        )

        self.assertEqual(response.code, 200)
        list_call = self.db_mock.fetch_all.call_args
        params = list_call.args[1]
        self.assertNotIn(OTHER_USER_ID, params)

    # ------------------------------------------------------------------
    # SEC-09: detail заявки — scoping для worker'а
    # ------------------------------------------------------------------

    def test_worker_get_foreign_request_not_found(self):
        """Worker GET чужой заявки → 404 (эксплойт SEC-09, не раскрываем существование)."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(WORKER_ID, "worker"),                 # auth
            _detail_row(5, OTHER_USER_ID, LOCKER_FOREIGN),  # get_by_id (чужая заявка)
        ]

        response = self.fetch(
            "/api/v1/emergency-request/5", method="GET",
            headers={"X-Remote-User": str(WORKER_ID)},
        )

        self.assertEqual(response.code, 404)

    def test_worker_get_own_request_success(self):
        """Worker GET своей заявки → 200."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(WORKER_ID, "worker"),            # auth
            _detail_row(5, WORKER_ID, LOCKER_GRANTED),  # get_by_id (своя заявка)
        ]

        response = self.fetch(
            "/api/v1/emergency-request/5", method="GET",
            headers={"X-Remote-User": str(WORKER_ID)},
        )

        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], 5)

    def test_operator_get_foreign_request_success(self):
        """Operator GET чужой заявки → 200 (видит все)."""
        self.db_mock.fetch_one.side_effect = [
            _user_row(OTHER_USER_ID, "operator"),     # auth
            _detail_row(5, WORKER_ID, LOCKER_GRANTED),  # get_by_id (чужая заявка)
        ]

        response = self.fetch(
            "/api/v1/emergency-request/5", method="GET",
            headers={"X-Remote-User": str(OTHER_USER_ID)},
        )

        self.assertEqual(response.code, 200)
