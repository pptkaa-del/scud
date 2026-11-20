"""Unit-тесты для API шкафов"""
from datetime import datetime
import json
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.locker_data.db.models.locker import Connectivity, DoorState, OperationalStatus
from app.main import make_app


class TestLockerApi(AsyncHTTPTestCase):
    def get_app(self):
        # Создаем моки БД и Command Queue напрямую
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()

        self.command_queue_mock = MagicMock()
        self.command_queue_mock.register_connection = AsyncMock()
        self.command_queue_mock.unregister_connection = AsyncMock()
        self.command_queue_mock.is_connected = MagicMock(return_value=False)
        self.command_queue_mock.send_command = AsyncMock()

        return make_app(self.db_mock, self.command_queue_mock)

    def setUp(self):
        super().setUp()
        # Clear mock return values and side effects before each test
        self.db_mock.fetch_one.return_value = None
        self.db_mock.fetch_one.side_effect = None
        self.db_mock.fetch_all.return_value = []
        self.db_mock.fetch_all.side_effect = None
        self.db_mock.execute.return_value = 0
        self.db_mock.execute.side_effect = None

        self.auth_user_row = {
            "id": 1, "phone": "+79123456789", "full_name": "Admin", "contractor_id": None,
            "created_at": datetime.now(), "created_by": None, "updated_at": datetime.now(),
            "updated_by": None, "state": "active"
        }
        self.test_locker = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "inventory_number": "СО №777",
            "location_address": "ул. Тестовая, 1",
            "location_lat": 55.751244,
            "location_lon": 37.618423,
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
            "description": "Тестовый шкаф",
            "metadata": {}
        }

    def test_unauthorized_missing_header(self):
        """Тест: отсутствует заголовок X-Remote-User"""
        response = self.fetch("/api/v1/lockers", method="GET")
        self.assertEqual(response.code, 401)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Unauthorized")
        self.assertEqual(data["status"], 401)
        self.assertIn("Missing X-Remote-User", data["detail"])

    def test_unauthorized_invalid_header(self):
        """Тест: невалидный заголовок X-Remote-User"""
        response = self.fetch("/api/v1/lockers", method="GET", headers={"X-Remote-User": "abc"})
        self.assertEqual(response.code, 401)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Unauthorized")
        self.assertEqual(data["status"], 401)
        self.assertIn("Invalid X-Remote-User", data["detail"])

    def test_get_lockers_list_success(self):
        """Тест: успешное получение списка шкафов"""
        self.db_mock.fetch_all.return_value = [self.test_locker]
        self.db_mock.fetch_one.return_value = {"count": 1}

        response = self.fetch("/api/v1/lockers", method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["inventory_number"], "СО №777")
        self.assertEqual(data["total"], 1)
        self.assertIn("limit", data)
        self.assertIn("offset", data)

    def test_get_locker_by_id_success(self):
        """Тест: успешное получение шкафа по ID"""
        self.db_mock.fetch_one.return_value = self.test_locker  # target locker

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], locker_id)
        self.assertEqual(data["inventory_number"], "СО №777")
        # last_opened_at должен присутствовать в ответе (null для нового шкафа)
        self.assertIn("last_opened_at", data)
        self.assertIsNone(data["last_opened_at"])
        # who_open_last также присутствует и null для шкафа без OPEN-команд
        self.assertIn("who_open_last", data)
        self.assertIsNone(data["who_open_last"])

    def test_get_locker_by_id_returns_who_open_last(self):
        """Тест: who_open_last в ответе содержит ID последнего открывавшего"""
        opened_locker = dict(self.test_locker)
        opened_locker["who_open_last"] = 42
        opened_locker["last_opened_at"] = datetime.now()
        self.db_mock.fetch_one.return_value = opened_locker

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["who_open_last"], 42)
        self.assertIsNotNone(data["last_opened_at"])

    def test_get_locker_not_found(self):
        """Тест: шкаф не найден"""
        self.db_mock.fetch_one.return_value = None  # locker not found

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["title"], "Not Found")
        self.assertEqual(data["status"], 404)
        self.assertIn("not found", data["detail"].lower())

    def test_get_locker_non_uuid_path_returns_404(self):
        """VAL-01: GET /locker/{не-UUID} → 404, а не 500.

        Невалидный UUID уходил в WHERE id = %s по uuid-колонке, psycopg2 поднимал
        22P02 (InvalidTextRepresentation) → 500. Мок воспроизводит это: если бы
        handler дошёл до БД, она бы бросила. parse_uuid_or_404 отбивает 404 ДО БД,
        поэтому мок не вызывается.
        """
        import psycopg2.errors
        self.db_mock.fetch_one.side_effect = psycopg2.errors.InvalidTextRepresentation(
            'invalid input syntax for type uuid: "not-a-uuid"'
        )
        response = self.fetch("/api/v1/locker/not-a-uuid", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 404)

    def test_delete_locker_non_uuid_path_returns_404(self):
        """VAL-01: DELETE /locker/{не-UUID} → 404, а не 500.

        Мок-БД бросает 22P02 как реальный Postgres — на старом коде это 500;
        с parse_uuid_or_404 до БД дело не доходит.
        """
        import psycopg2.errors
        self.db_mock.fetch_one.side_effect = psycopg2.errors.InvalidTextRepresentation(
            'invalid input syntax for type uuid: "zzz"'
        )
        response = self.fetch("/api/v1/locker/zzz", method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    def test_create_locker_success(self):
        """Тест: успешное создание шкафа (mqtt — единственный создаваемый транспорт)."""
        from unittest.mock import patch

        new_locker = self.test_locker.copy()
        new_locker["id"] = "660e8400-e29b-41d4-a716-446655440001"
        new_locker["inventory_number"] = "СО №888"
        new_locker["transport"] = "mqtt"
        new_locker["relay_id"] = 888

        # fetch_one: 1) inventory check (None) 2) relay_id check (None).
        self.db_mock.fetch_one.side_effect = [None, None]
        self.db_mock.fetch_one_strict.return_value = new_locker  # INSERT RETURNING *

        body = json.dumps({
            "inventory_number": "СО №888",
            "transport": "mqtt",
            "relay_id": 888,
            "location_address": "ул. Новая, 2"
        })

        with patch("app.handlers.locker.asyncio.to_thread",
                   new_callable=AsyncMock) as to_thread:
            to_thread.return_value = "deadbeef"
            response = self.fetch("/api/v1/lockers", method="POST",
                                  headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                                  body=body)
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["inventory_number"], "СО №888")
        self.assertNotIn("auth_key", data)

    def test_create_mqtt_locker_provisions_via_to_thread(self):
        """Fix #8: provision_device уводится в asyncio.to_thread (блокирующий
        fcntl.flock + файловый I/O не должен блокировать event loop)."""
        from unittest.mock import patch

        new_locker = self.test_locker.copy()
        new_locker["id"] = "660e8400-e29b-41d4-a716-446655440001"
        new_locker["inventory_number"] = "СО №mqtt"
        new_locker["transport"] = "mqtt"
        new_locker["relay_id"] = 42

        # fetch_one: 1) inventory check (None) 2) relay_id check (None).
        self.db_mock.fetch_one.side_effect = [None, None]
        self.db_mock.fetch_one_strict.return_value = new_locker

        body = json.dumps({
            "inventory_number": "СО №mqtt", "transport": "mqtt", "relay_id": 42,
        })

        with patch("app.handlers.locker.provisioning.provision_device",
                   return_value="deadbeef") as prov, \
                patch("app.handlers.locker.asyncio.to_thread",
                      new_callable=AsyncMock) as to_thread:
            to_thread.return_value = "deadbeef"
            response = self.fetch(
                "/api/v1/lockers", method="POST",
                headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                body=body)
            self.assertEqual(response.code, 201)
            # provision_device вызван именно через to_thread (offload), а не напрямую.
            to_thread.assert_awaited_once()
            args = to_thread.call_args.args
            self.assertIs(args[0], prov)
            self.assertEqual(args[1], 42)

    def test_create_mqtt_locker_provision_failure_compensates(self):
        """Fix #7: сбой provisioning ПОСЛЕ коммита locker'а компенсируется
        soft-delete только что созданной записи и возвращает 5xx (не orphaned
        шкаф + не тихий 500)."""
        from unittest.mock import patch

        new_locker = self.test_locker.copy()
        new_locker["id"] = "660e8400-e29b-41d4-a716-446655440099"
        new_locker["inventory_number"] = "СО №fail"
        new_locker["transport"] = "mqtt"
        new_locker["relay_id"] = 7

        self.db_mock.fetch_one.side_effect = [None, None]
        self.db_mock.fetch_one_strict.return_value = new_locker
        # repo.delete (компенсация) бьёт UPDATE ... RETURNING id через fetch_one
        # с read_only=False — добавим в side_effect возврат для delete.
        self.db_mock.fetch_one.side_effect = [None, None, {"id": new_locker["id"]}]

        body = json.dumps({
            "inventory_number": "СО №fail", "transport": "mqtt", "relay_id": 7,
        })

        with patch("app.handlers.locker.asyncio.to_thread",
                   new_callable=AsyncMock) as to_thread:
            to_thread.side_effect = OSError("flock failed")
            response = self.fetch(
                "/api/v1/lockers", method="POST",
                headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                body=body)
            # 5xx, не orphaned 201.
            self.assertGreaterEqual(response.code, 500)
            # Компенсация: locker удалён (soft-delete UPDATE lockers SET state).
            delete_call = self.db_mock.fetch_one.await_args_list[-1]
            sql = delete_call.args[0]
            self.assertIn("UPDATE lockers", sql)
            self.assertIn("SET state = %s", sql)
            # И именно тот locker, что был создан.
            self.assertEqual(delete_call.args[1][1], new_locker["id"])

    def test_create_locker_missing_inventory_number(self):
        """Тест: попытка создания шкафа без инвентарного номера"""
        body = json.dumps({
            "location_address": "ул. Новая, 2"
        })

        response = self.fetch("/api/v1/lockers", method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 400)

    def test_create_locker_duplicate_inventory_number(self):
        """Тест: попытка создания шкафа с существующим инвентарным номером"""
        self.db_mock.fetch_one.return_value = self.test_locker  # existing locker with same inventory_number

        body = json.dumps({
            "inventory_number": "СО №777",
            "transport": "mqtt",
            "relay_id": 777,
            "location_address": "ул. Новая, 2"
        })

        response = self.fetch("/api/v1/lockers", method="POST",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 409)

    def test_update_locker_state_success(self):
        """Тест: успешное обновление состояния шкафа"""
        updated_locker = self.test_locker.copy()
        updated_locker["operational_status"] = OperationalStatus.MAINTENANCE

        self.db_mock.fetch_one.return_value = updated_locker  # updated locker

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({"operational_status": "maintenance"})

        response = self.fetch(f"/api/v1/locker/{locker_id}/state", method="PUT",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["operational_status"], "maintenance")

    def test_delete_locker_success(self):
        """Тест: успешное удаление шкафа"""
        self.db_mock.fetch_one.return_value = {"id": "550e8400-e29b-41d4-a716-446655440000"}  # deleted row

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(f"/api/v1/locker/{locker_id}", method="DELETE",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 204)

    def test_update_locker_success(self):
        """Тест: успешное обновление данных шкафа"""
        updated_locker = self.test_locker.copy()
        updated_locker["location_address"] = "ул. Обновленная, 10"
        updated_locker["description"] = "Обновленное описание"

        self.db_mock.fetch_one.return_value = updated_locker

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({
            "location_address": "ул. Обновленная, 10",
            "description": "Обновленное описание"
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}", method="PUT",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["location_address"], "ул. Обновленная, 10")
        self.assertEqual(data["description"], "Обновленное описание")

    def test_update_locker_not_found(self):
        """Тест: обновление несуществующего шкафа"""
        self.db_mock.fetch_one.return_value = None

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({
            "location_address": "ул. Обновленная, 10"
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}", method="PUT",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 404)

    def test_get_lockers_with_state_filter(self):
        """Тест: получение списка шкафов с фильтрацией по connectivity"""
        online_locker = self.test_locker.copy()
        online_locker["connectivity"] = Connectivity.ONLINE

        self.db_mock.fetch_all.return_value = [online_locker]
        self.db_mock.fetch_one.return_value = {"count": 1}

        response = self.fetch("/api/v1/lockers?connectivity=online", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["connectivity"], "online")

    def test_update_locker_state_invalid_value(self):
        """Тест: обновление состояния шкафа с некорректным значением"""
        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({"operational_status": "invalid_status"})

        response = self.fetch(f"/api/v1/locker/{locker_id}/state", method="PUT",
                              headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                              body=body)
        self.assertEqual(response.code, 400)

    def test_create_locker_no_auth_key_in_response(self):
        """Тест: ответ создания шкафа не содержит auth_key (удалён)"""
        from unittest.mock import patch

        new_locker = self.test_locker.copy()
        new_locker["id"] = "660e8400-e29b-41d4-a716-446655440001"
        new_locker["inventory_number"] = "СО №999"
        new_locker["transport"] = "mqtt"
        new_locker["relay_id"] = 999

        # fetch_one: 1) inventory check (None) 2) relay_id check (None).
        self.db_mock.fetch_one.side_effect = [None, None]
        self.db_mock.fetch_one_strict.return_value = new_locker  # INSERT RETURNING *

        body = json.dumps({
            "inventory_number": "СО №999", "transport": "mqtt", "relay_id": 999,
        })
        with patch("app.handlers.locker.asyncio.to_thread",
                   new_callable=AsyncMock) as to_thread:
            to_thread.return_value = "deadbeef"
            response = self.fetch("/api/v1/lockers", method="POST",
                                  headers={"X-Remote-User": "1", "Content-Type": "application/json"},
                                  body=body)
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertNotIn("auth_key", data)

    def test_get_lockers_view_location_returns_only_id_and_coords(self):
        """Тест: view=location возвращает id, координаты и operational_status"""
        location_row = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "location_lat": 55.751244,
            "location_lon": 37.618423,
            "location_address": "ул. Тестовая, 1",
            "operational_status": OperationalStatus.NORMAL,
        }
        self.db_mock.fetch_all.return_value = [location_row]
        self.db_mock.fetch_one.return_value = {"count": 1}

        response = self.fetch("/api/v1/lockers?view=location", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(len(data["items"]), 1)
        item = data["items"][0]
        self.assertEqual(
            set(item.keys()),
            {"id", "location_lat", "location_lon", "location_address", "operational_status"},
        )
        self.assertEqual(item["id"], location_row["id"])
        self.assertEqual(item["location_lat"], 55.751244)
        self.assertEqual(item["location_lon"], 37.618423)
        self.assertEqual(item["location_address"], "ул. Тестовая, 1")
        self.assertEqual(item["operational_status"], "normal")
        self.assertNotIn("inventory_number", item)
        self.assertNotIn("connectivity", item)
        self.assertNotIn("metadata", item)

    def test_get_lockers_view_location_includes_operational_status(self):
        """Тест: operational_status корректно сериализуется (alarm/maintenance)"""
        rows = [
            {
                "id": "550e8400-e29b-41d4-a716-446655440000",
                "location_lat": 55.0,
                "location_lon": 37.0,
                "location_address": "A",
                "operational_status": OperationalStatus.ALARM,
            },
            {
                "id": "550e8400-e29b-41d4-a716-446655440001",
                "location_lat": 56.0,
                "location_lon": 38.0,
                "location_address": "B",
                "operational_status": OperationalStatus.MAINTENANCE,
            },
        ]
        self.db_mock.fetch_all.return_value = rows
        self.db_mock.fetch_one.return_value = {"count": 2}

        response = self.fetch("/api/v1/lockers?view=location", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        statuses = [item["operational_status"] for item in data["items"]]
        self.assertEqual(statuses, ["alarm", "maintenance"])

    def test_get_lockers_view_location_with_filters_and_pagination(self):
        """Тест: view=location совместим с фильтрами и пагинацией"""
        location_row = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "location_lat": 55.751244,
            "location_lon": 37.618423,
            "location_address": None,
            "operational_status": OperationalStatus.NORMAL,
        }
        self.db_mock.fetch_all.return_value = [location_row]
        self.db_mock.fetch_one.return_value = {"count": 7}

        response = self.fetch(
            "/api/v1/lockers?view=location&connectivity=online&limit=10&offset=5",
            method="GET",
            headers={"X-Remote-User": "1"},
        )
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["total"], 7)
        self.assertEqual(data["limit"], 10)
        self.assertEqual(data["offset"], 5)
        self.assertIsNone(data["items"][0]["location_address"])

    def test_get_lockers_view_invalid_returns_400(self):
        """Тест: невалидный view → 400"""
        response = self.fetch("/api/v1/lockers?view=garbage", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 400)
        data = json.loads(response.body)
        self.assertEqual(data["status"], 400)
        self.assertIn("view", data["detail"])

    def test_get_lockers_no_view_returns_full_object(self):
        """Тест регрессии: без view возвращается полный Locker со всеми полями"""
        self.db_mock.fetch_all.return_value = [self.test_locker]
        self.db_mock.fetch_one.return_value = {"count": 1}

        response = self.fetch("/api/v1/lockers", method="GET",
                              headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        item = data["items"][0]
        # Поля полного Locker должны присутствовать (а не урезанные)
        self.assertIn("inventory_number", item)
        self.assertIn("connectivity", item)
        self.assertIn("metadata", item)
        self.assertIn("created_at", item)
