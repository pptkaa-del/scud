"""Списочные эндпоинты отдают только шкафы с Допуском (row-scoping).

ACM гейтит коллекцию ``GET /api/v1/lockers`` только по роли: в её пути нет
UUID, поэтому ``is_locker_scoped`` → None и Допуск на URL-уровне не проверяется
(и не может — шкафы лежат в чужой для ACM БД). Ответ на «какие строки видно»
несёт заголовок ``X-Remote-Lockers``, и отфильтровать их обязан locker_api.

Ключевые инварианты:
  * worker видит ровно свои шкафы; ``total`` считается по той же области, что
    и ``items`` (иначе пагинация обещает страницы, которых нет);
  * нет Допусков → 200 + пустой список, НЕ 403 (403 сломал бы session-scoped
    probe в tests/integration/conftest.py, принимающий только 200/401);
  * operator/administrator/full_access → SQL без id-фильтра (нулевая
    регрессия на горячем пути);
  * неизвестная роль → пусто, а не весь парк (fail-closed).
"""

from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.locker_data.db.models.locker import (
    Connectivity,
    DoorState,
    OperationalStatus,
)
from app.main import make_app

LOCKER_A = "550e8400-e29b-41d4-a716-446655440000"
LOCKER_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
A_DASHLESS = "550e8400e29b41d4a716446655440000"

_ANY_CLAUSE = "id = ANY(%s::uuid[])"


def _make_locker_row(locker_id: str = LOCKER_A) -> dict:
    return {
        "id": locker_id,
        "inventory_number": "СО №777",
        "location_address": "ул. Тестовая, 1",
        "location_lat": None,
        "location_lon": None,
        "public_key": None,
        "created_at": datetime.now(),
        "created_by": 1,
        "updated_at": datetime.now(),
        "updated_by": 1,
        "connectivity": Connectivity.OFFLINE,
        "door": DoorState.CLOSED,
        "operational_status": OperationalStatus.NORMAL,
        "last_seen_at": None,
        "last_opened_at": None,
        "who_open_last": None,
        "description": None,
        "metadata": {},
    }


class TestLockerListScope(AsyncHTTPTestCase):
    """Repo замокан: ассертим SQL и параметры, которые собрал handler."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock(return_value={"count": 0})
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.db_mock.execute = AsyncMock()

        self.cq_mock = MagicMock()
        self.cq_mock.register_connection = AsyncMock()
        self.cq_mock.unregister_connection = AsyncMock()
        self.cq_mock.is_connected = MagicMock(return_value=False)
        self.cq_mock.send_command = AsyncMock()

        return make_app(self.db_mock, self.cq_mock)

    def _get(self, headers):
        return self.fetch("/api/v1/lockers", method="GET", headers=headers)

    def _get_all_sql(self):
        return self.db_mock.fetch_all.call_args[0][0]

    def _get_all_params(self):
        return self.db_mock.fetch_all.call_args[0][1]

    def _count_sql(self):
        return self.db_mock.fetch_one.call_args[0][0]

    def _count_params(self):
        return self.db_mock.fetch_one.call_args[0][1]

    # ── worker: область применяется ──────────────────────────────────────

    def test_worker_scope_adds_any_clause_to_get_all(self):
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "worker",
                          "X-Remote-Lockers": f"{LOCKER_A},{LOCKER_B}"})
        self.assertEqual(resp.code, 200)
        self.assertIn(_ANY_CLAUSE, self._get_all_sql())
        self.assertIn(sorted([LOCKER_A, LOCKER_B]), list(self._get_all_params()))

    def test_worker_scope_adds_any_clause_to_count_too(self):
        """total обязан считаться по той же области, что и items."""
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "worker",
                          "X-Remote-Lockers": LOCKER_A})
        self.assertEqual(resp.code, 200)
        self.assertIn(_ANY_CLAUSE, self._count_sql())
        self.assertIn([LOCKER_A], list(self._count_params()))

    def test_worker_scope_param_order_places_ids_before_limit_offset(self):
        """Builder позиционный: id-массив обязан идти до limit/offset."""
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "worker",
                          "X-Remote-Lockers": LOCKER_A})
        self.assertEqual(resp.code, 200)
        params = list(self._get_all_params())
        self.assertEqual(params[-2:], [100, 0])
        self.assertEqual(params[-3], [LOCKER_A])

    def test_worker_with_empty_scope_returns_200_empty_items_total_zero(self):
        """Нет Допусков → пусто, но 200 (403 сломал бы probe в conftest)."""
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "worker",
                          "X-Remote-Lockers": ""})
        self.assertEqual(resp.code, 200)
        data = json.loads(resp.body)
        self.assertEqual(data["items"], [])
        self.assertEqual(data["total"], 0)
        # Фильтр в ноль строк, а не отсутствие фильтра.
        self.assertIn(_ANY_CLAUSE, self._get_all_sql())
        self.assertIn([], list(self._get_all_params()))

    def test_worker_with_dashless_uuid_grant_sends_canonical_param(self):
        """SEC-02: грант в не-каноническом виде канонизируется до SQL."""
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "worker",
                          "X-Remote-Lockers": A_DASHLESS})
        self.assertEqual(resp.code, 200)
        self.assertIn([LOCKER_A], list(self._get_all_params()))

    def test_worker_scope_survives_other_filters(self):
        """Область комбинируется с q/connectivity, а не подменяется ими."""
        resp = self.fetch(
            "/api/v1/lockers?q=abc&connectivity=online", method="GET",
            headers={"X-Remote-User": "1", "X-Remote-Role": "worker",
                     "X-Remote-Lockers": LOCKER_A},
        )
        self.assertEqual(resp.code, 200)
        sql = self._get_all_sql()
        self.assertIn(_ANY_CLAUSE, sql)
        self.assertIn("ILIKE", sql)

    # ── unrestricted: нулевая регрессия ──────────────────────────────────

    def test_worker_with_full_access_marker_generates_no_any_clause(self):
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "worker",
                          "X-Remote-Lockers": "*"})
        self.assertEqual(resp.code, 200)
        self.assertNotIn(_ANY_CLAUSE, self._get_all_sql())

    def test_operator_generates_no_any_clause(self):
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "operator",
                          "X-Remote-Lockers": "*"})
        self.assertEqual(resp.code, 200)
        self.assertNotIn(_ANY_CLAUSE, self._get_all_sql())

    def test_administrator_ignores_locker_header_entirely(self):
        """Роль решает раньше заголовка: даже узкий список не сужает админа."""
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "administrator",
                          "X-Remote-Lockers": LOCKER_A})
        self.assertEqual(resp.code, 200)
        self.assertNotIn(_ANY_CLAUSE, self._get_all_sql())
        self.assertNotIn(_ANY_CLAUSE, self._count_sql())

    # ── fail-closed ──────────────────────────────────────────────────────

    def test_unknown_role_returns_200_empty_not_whole_fleet(self):
        """Неизвестная роль не открывает парк (fail-closed)."""
        resp = self._get({"X-Remote-User": "1", "X-Remote-Role": "root",
                          "X-Remote-Lockers": ""})
        self.assertEqual(resp.code, 200)
        self.assertIn(_ANY_CLAUSE, self._get_all_sql())
        self.assertIn([], list(self._get_all_params()))

    def test_absent_role_and_header_returns_empty_not_whole_fleet(self):
        """Форма запроса, которую gateway не может прислать → пусто, не всё."""
        resp = self._get({"X-Remote-User": "1"})
        self.assertEqual(resp.code, 200)
        self.assertIn(_ANY_CLAUSE, self._get_all_sql())
        self.assertIn([], list(self._get_all_params()))


class TestLockerBatchScope(AsyncHTTPTestCase):
    """POST /lockers/batch — та же область (правило ACM operator+, но
    defense-in-depth не должен зависеть только от ACM)."""

    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock(return_value={"count": 0})
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.db_mock.execute = AsyncMock()

        self.cq_mock = MagicMock()
        self.cq_mock.is_connected = MagicMock(return_value=False)
        self.cq_mock.send_command = AsyncMock()
        return make_app(self.db_mock, self.cq_mock)

    def _post(self, locker_ids, headers):
        return self.fetch(
            "/api/v1/lockers/batch", method="POST",
            headers={"Content-Type": "application/json", **headers},
            body=json.dumps({"locker_ids": locker_ids}),
        )

    def test_batch_worker_scope_intersects_requested_ids(self):
        """worker не вытащит батчем шкаф вне своих Допусков."""
        resp = self._post(
            [LOCKER_A, LOCKER_B],
            {"X-Remote-User": "1", "X-Remote-Role": "worker",
             "X-Remote-Lockers": LOCKER_A},
        )
        self.assertEqual(resp.code, 200)
        params = list(self.db_mock.fetch_all.call_args[0][1])
        self.assertIn([LOCKER_A], params)

    def test_batch_administrator_passes_ids_through_unchanged(self):
        resp = self._post(
            [LOCKER_A, LOCKER_B],
            {"X-Remote-User": "1", "X-Remote-Role": "administrator",
             "X-Remote-Lockers": "*"},
        )
        self.assertEqual(resp.code, 200)
        params = list(self.db_mock.fetch_all.call_args[0][1])
        self.assertIn([LOCKER_A, LOCKER_B], params)
