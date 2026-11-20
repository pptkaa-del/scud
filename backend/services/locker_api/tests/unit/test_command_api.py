"""Unit-тесты для API команд"""
from datetime import datetime
import json
import unittest.mock
from unittest.mock import AsyncMock, MagicMock

from tornado.testing import AsyncHTTPTestCase

from app.locker_data.db.models.command import CommandStatus, CommandType
from app.main import make_app


class TestCommandApi(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock()
        self.db_mock.fetch_one_strict = AsyncMock()
        self.db_mock.fetch_all = AsyncMock()
        self.db_mock.execute = AsyncMock()

        self.command_queue_mock = MagicMock()
        self.command_queue_mock.is_connected.return_value = False
        self.command_queue_mock.send_command = AsyncMock(return_value=True)
        self.command_queue_mock.command_ttl_s = 60
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
            "id": 1, "phone": "+79123456789", "full_name": "Admin",
            "contractor_id": None, "created_at": datetime.now(),
            "created_by": None, "updated_at": datetime.now(),
            "updated_by": None, "state": "active"
        }
        self.test_locker = {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "inventory_number": "СО №777", "location_address": "ул. Тестовая, 1",
            "location_lat": 55.751244, "location_lon": 37.618423,
            "auth_key": "test_key", "created_at": datetime.now(),
            "created_by": 1, "updated_at": datetime.now(),
            "updated_by": 1, "connectivity": "offline", "door": "closed",
            "operational_status": "normal",
            "last_seen_at": None, "last_opened_at": None, "description": "Тест", "metadata": {}
        }
        self.test_command = {
            "id": "770e8400-e29b-41d4-a716-446655440002",
            "locker_id": "550e8400-e29b-41d4-a716-446655440000",
            "type": CommandType.OPEN, "status": CommandStatus.PENDING,
            "parameters": {}, "created_at": datetime.now(),
            "created_by": 1, "sent_at": None, "delivered_at": None,
            "executed_at": None, "failed_at": None,
            "result": {}, "error_message": None,
            "retry_count": 0, "max_retries": 3
        }

    def test_get_commands_list_success(self):
        """Тест: получение истории команд (paginated envelope)."""
        # locker_check, count(), затем fetch_all для get_commands_by_locker
        self.db_mock.fetch_one.side_effect = [self.test_locker, {"count": 1}]
        self.db_mock.fetch_all.return_value = [self.test_command]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="GET",
            headers={"X-Remote-User": "1", "X-Remote-Lockers": "*"},
        )
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["limit"], 100)
        self.assertEqual(data["offset"], 0)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["type"], "open")

    def test_create_command_success(self):
        """Тест: создание команды (admin role — разрешено всё)."""
        new_command = self.test_command.copy()

        self.db_mock.fetch_one.side_effect = [self.test_locker]  # locker exists
        self.db_mock.fetch_one_strict.return_value = new_command  # INSERT RETURNING *

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({
            "type": "open",
            "parameters": {}
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}/commands",
                              method="POST",
                              headers={
                                  "X-Remote-User": "1",
                                  "X-Remote-Role": "administrator",
                                  "Content-Type": "application/json",
                              },
                              body=body)
        self.assertEqual(response.code, 201)
        data = json.loads(response.body)
        self.assertEqual(data["type"], "open")
        self.assertEqual(data["status"], "pending")

    def test_create_command_invalid_type(self):
        """Тест: создание команды с неверным типом"""
        self.db_mock.fetch_one.return_value = self.test_locker  # locker exists

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        body = json.dumps({
            "type": "invalid_command"
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}/commands",
                              method="POST",
                              headers={
                                  "X-Remote-User": "1",
                                  "X-Remote-Role": "administrator",
                                  "Content-Type": "application/json",
                              },
                              body=body)
        self.assertEqual(response.code, 400)

    # ------------------------------------------------------------------
    # privilege escalation matrix
    # ------------------------------------------------------------------
    def test_create_command_worker_open_allowed(self):
        """worker может open."""
        new_command = self.test_command.copy()
        self.db_mock.fetch_one.side_effect = [self.test_locker]
        self.db_mock.fetch_one_strict.return_value = new_command

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "worker",
                "X-Remote-Lockers": locker_id,
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "open", "parameters": {}}),
        )
        self.assertEqual(response.code, 201)

    def test_create_command_worker_reboot_forbidden(self):
        """worker НЕ может reboot — 403, команда не создана.

        Проверяем что НИ locker get_by_id (fetch_one), НИ create (execute)
        не были вызваны — permission check идёт ДО любого DB-touch.
        """
        self.db_mock.fetch_one.return_value = self.test_locker
        self.db_mock.execute.reset_mock()
        self.db_mock.fetch_one.reset_mock()

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "worker",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "reboot", "parameters": {}}),
        )
        self.assertEqual(response.code, 403)
        # locker existence check (fetch_one) ДО permission не делается.
        self.db_mock.fetch_one.assert_not_called()
        # command_repo.create (через db.execute INSERT) тоже не должен
        # был сработать — иначе команда сохранится несмотря на 403.
        self.db_mock.execute.assert_not_called()

    def test_create_command_worker_config_forbidden(self):
        """worker НЕ может config — 403, команда не создана."""
        self.db_mock.execute.reset_mock()
        self.db_mock.fetch_one.reset_mock()

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "worker",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "config", "parameters": {}}),
        )
        self.assertEqual(response.code, 403)
        self.db_mock.fetch_one.assert_not_called()
        self.db_mock.execute.assert_not_called()

    def test_create_command_operator_reboot_allowed(self):
        """operator может reboot."""
        new_command = self.test_command.copy()
        new_command["type"] = "reboot"
        self.db_mock.fetch_one.side_effect = [self.test_locker]
        self.db_mock.fetch_one_strict.return_value = new_command

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "operator",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "reboot", "parameters": {}}),
        )
        self.assertEqual(response.code, 201)

    def test_create_command_role_uppercase_normalized(self):
        """Defense-in-depth: WORKER (uppercase) приводится к worker и работает.

        ACM сейчас отдаёт lowercase, но если issuer когда-то напишет
        «Worker»/«WORKER» — handler нормализует через .lower() и не
        отказывает легитимному worker'у в open.
        """
        new_command = self.test_command.copy()
        self.db_mock.fetch_one.side_effect = [self.test_locker]
        self.db_mock.fetch_one_strict.return_value = new_command

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "WORKER",
                "X-Remote-Lockers": locker_id,
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "open", "parameters": {}}),
        )
        self.assertEqual(response.code, 201)

    def test_create_command_missing_role_forbidden(self):
        """Отсутствие X-Remote-Role → 403 (default deny), команда не создана."""
        self.db_mock.execute.reset_mock()
        self.db_mock.fetch_one.reset_mock()

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "open", "parameters": {}}),
        )
        self.assertEqual(response.code, 403)
        self.db_mock.fetch_one.assert_not_called()
        self.db_mock.execute.assert_not_called()

    def test_inline_dispatch_prepares_send_before_send(self):
        """Fix BUG 3 + altitude #10: inline-доставка mqtt-команды зовёт
        prepare_send (транспорт-owned pre-send prep) ДО send_command, чтобы
        payload содержал relay_cmd_id.

        Без prepare_send MqttTransport.send_command падает на
        KeyError('relay_cmd_id'). Эмулируем подключённый mqtt-локер
        (is_connected=True) и prepare_send, вставляющий relay_cmd_id в
        command_data; проверяем порядок и наличие ключа.
        """
        new_command = self.test_command.copy()
        sent_command = self.test_command.copy()
        sent_command["status"] = CommandStatus.SENT
        # fetch_one: 1) locker existence  2) update_status_to_sent RETURNING *.
        self.db_mock.fetch_one.side_effect = [self.test_locker, sent_command]
        self.db_mock.fetch_one_strict.return_value = new_command

        order: list[str] = []

        async def fake_prepare(command, command_data):
            order.append("prepare")
            command_data["relay_cmd_id"] = 0xABCD

        sent_payloads: list[dict] = []

        async def fake_send(locker_id, command_data):
            order.append("send")
            sent_payloads.append(command_data)
            return True

        self.command_queue_mock.is_connected.return_value = True
        self.command_queue_mock.prepare_send = AsyncMock(side_effect=fake_prepare)
        self.command_queue_mock.send_command = AsyncMock(side_effect=fake_send)

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "administrator",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "open", "parameters": {}}),
        )
        self.assertEqual(response.code, 201)
        # prepare ОБЯЗАТЕЛЬНО до send, и relay_cmd_id попал в отправленный payload.
        self.assertEqual(order, ["prepare", "send"])
        self.assertEqual(len(sent_payloads), 1)
        self.assertEqual(sent_payloads[0]["relay_cmd_id"], 0xABCD)

    def test_inline_dispatch_failure_returns_201_does_not_send(self):
        """COR-02/18: claim-then-send. Команда заявлена (pending→sent), затем
        prepare_send падает. Запрос НЕ роняется в 500 — команда уже закоммичена;
        claim возвращается в pending (release_claim) для повтора в пределах TTL.
        send не достигается (prepare упал раньше). Возвращаем 201."""
        new_command = self.test_command.copy()
        sent_command = self.test_command.copy()
        sent_command["status"] = CommandStatus.SENT
        # fetch_one: 1) locker existence  2) claim (update_status_to_sent
        # RETURNING) — claim проходит, дальше падает prepare_send.
        self.db_mock.fetch_one.side_effect = [self.test_locker, sent_command]
        self.db_mock.fetch_one_strict.return_value = new_command

        self.command_queue_mock.is_connected.return_value = True
        self.command_queue_mock.prepare_send = AsyncMock(
            side_effect=RuntimeError("relay_cmd_id allocation exhausted"))
        sent_calls: list = []

        async def fake_send(locker_id, command_data):
            sent_calls.append(command_data)
            return True

        self.command_queue_mock.send_command = AsyncMock(side_effect=fake_send)

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "administrator",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "open", "parameters": {}}),
        )
        # Команда создана (201), несмотря на сбой доставки после claim.
        self.assertEqual(response.code, 201)
        # send не достигнут (prepare упал раньше).
        self.assertEqual(sent_calls, [])
        self.assertTrue(self._release_claim_called())

    def _release_claim_called(self) -> bool:
        return any(
            "sent_at = NULL" in c.args[0] for c in self.db_mock.execute.call_args_list
        )

    def _outbox_statuses(self) -> list[str]:
        statuses = []
        for c in self.db_mock.fetch_one_strict.call_args_list:
            if "outbound_history_events" in c.args[0]:
                statuses.append(json.loads(c.args[1][2])["status"])
        return statuses

    def _post_open_to_connected_locker(self, send_result: bool):
        new_command = self.test_command.copy()
        sent_command = self.test_command.copy()
        sent_command["status"] = CommandStatus.SENT
        self.db_mock.fetch_one.side_effect = [self.test_locker, sent_command]
        self.db_mock.fetch_one_strict.side_effect = [new_command, {"id": 1}, {"id": 2}]
        self.command_queue_mock.is_connected.return_value = True
        self.command_queue_mock.prepare_send = AsyncMock()
        self.command_queue_mock.send_command = AsyncMock(return_value=send_result)
        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        with self.assertLogs(level="INFO") as logs:
            response = self.fetch(
                f"/api/v1/locker/{locker_id}/commands",
                method="POST",
                headers={
                    "X-Remote-User": "1",
                    "X-Remote-Role": "administrator",
                    "Content-Type": "application/json",
                },
                body=json.dumps({"type": "open", "parameters": {}}),
            )
        return response, logs.output

    def test_inline_send_false_no_sent_event_no_dispatched_log(self):
        response, output = self._post_open_to_connected_locker(send_result=False)
        self.assertEqual(response.code, 201)
        self.assertEqual(self._outbox_statuses(), ["pending"])
        self.assertFalse(any("immediately dispatched" in line for line in output))
        self.assertTrue(self._release_claim_called())
        self.db_mock.fetch_one.assert_any_call(
            unittest.mock.ANY,
            ("770e8400-e29b-41d4-a716-446655440002", 60),
            read_only=False,
        )

    def test_inline_send_true_publishes_sent(self):
        response, output = self._post_open_to_connected_locker(send_result=True)
        self.assertEqual(response.code, 201)
        self.assertEqual(self._outbox_statuses(), ["pending", "sent"])
        self.assertTrue(any("immediately dispatched" in line for line in output))
        self.assertFalse(self._release_claim_called())

    def test_inline_dispatch_skips_send_when_claim_lost(self):
        """COR-02/18: если claim проиграл (queue-processor успел заявить
        команду раньше → update_status_to_sent вернул None), inline-доставка
        НЕ отправляет команду повторно (no double-send)."""
        new_command = self.test_command.copy()
        # fetch_one: 1) locker existence  2) claim → None (проигран).
        self.db_mock.fetch_one.side_effect = [self.test_locker, None]
        self.db_mock.fetch_one_strict.return_value = new_command

        self.command_queue_mock.is_connected.return_value = True
        sent_calls: list = []

        async def fake_send(locker_id, command_data):
            sent_calls.append(command_data)
            return True

        self.command_queue_mock.prepare_send = AsyncMock()
        self.command_queue_mock.send_command = AsyncMock(side_effect=fake_send)

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "administrator",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "open", "parameters": {}}),
        )
        self.assertEqual(response.code, 201)
        # claim проигран → ни prepare, ни send не вызваны.
        self.assertEqual(sent_calls, [])
        self.command_queue_mock.prepare_send.assert_not_called()

    def test_get_command_detail_success(self):
        """Тест: получение информации о команде"""
        self.db_mock.fetch_one.return_value = self.test_command  # command

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        command_id = "770e8400-e29b-41d4-a716-446655440002"
        response = self.fetch(f"/api/v1/locker/{locker_id}/commands/{command_id}",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["id"], command_id)

    def test_get_command_detail_not_found(self):
        """Тест: команда не найдена"""
        self.db_mock.fetch_one.return_value = None

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        command_id = "770e8400-e29b-41d4-a716-446655440999"
        response = self.fetch(f"/api/v1/locker/{locker_id}/commands/{command_id}",
                              method="GET", headers={"X-Remote-User": "1"})
        self.assertEqual(response.code, 404)

    def test_create_command_locker_not_found(self):
        """Тест: создание команды для несуществующего шкафа"""
        self.db_mock.fetch_one.return_value = None  # locker not found

        locker_id = "550e8400-e29b-41d4-a716-446655440999"
        body = json.dumps({
            "type": "open",
            "parameters": {}
        })

        response = self.fetch(f"/api/v1/locker/{locker_id}/commands",
                              method="POST",
                              headers={
                                  "X-Remote-User": "1",
                                  "X-Remote-Role": "administrator",
                                  "Content-Type": "application/json",
                              },
                              body=body)
        self.assertEqual(response.code, 404)

    def test_config_retired_returns_403(self):
        """CONFIG выведен из эксплуатации → 403 для любой роли и любого
        транспорта. Permission-check идёт ДО locker lookup, поэтому DB не
        трогается (транспорт локера роли не играет)."""
        self.db_mock.fetch_one.reset_mock()

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        for role in ("operator", "administrator", "worker"):
            with self.subTest(role=role):
                response = self.fetch(
                    f"/api/v1/locker/{locker_id}/commands",
                    method="POST",
                    headers={
                        "X-Remote-User": "1",
                        "X-Remote-Role": role,
                        "Content-Type": "application/json",
                    },
                    body=json.dumps({"type": "config", "parameters": {}}),
                )
                self.assertEqual(response.code, 403)
        # config отклонён до всякого обращения к БД.
        self.db_mock.fetch_one.assert_not_called()

    def test_open_allowed_for_mqtt_locker(self):
        """OPEN для mqtt-локера проходит: транспорт-специфичных отказов нет."""
        mqtt_locker = {**self.test_locker, "transport": "mqtt", "relay_id": 1}
        self.db_mock.fetch_one.side_effect = [mqtt_locker]
        self.db_mock.fetch_one_strict.return_value = self.test_command

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands",
            method="POST",
            headers={
                "X-Remote-User": "1",
                "X-Remote-Role": "operator",
                "Content-Type": "application/json",
            },
            body=json.dumps({"type": "open", "parameters": {}}),
        )
        self.assertEqual(response.code, 201)

    def test_get_commands_with_status_filter(self):
        """Тест: получение команд с фильтрацией по статусу (paginated envelope)."""
        pending_command = self.test_command.copy()
        pending_command["status"] = CommandStatus.PENDING

        self.db_mock.fetch_one.side_effect = [self.test_locker, {"count": 1}]
        self.db_mock.fetch_all.return_value = [pending_command]

        locker_id = "550e8400-e29b-41d4-a716-446655440000"
        response = self.fetch(
            f"/api/v1/locker/{locker_id}/commands?status=pending",
            method="GET",
            headers={"X-Remote-User": "1", "X-Remote-Lockers": "*"},
        )
        self.assertEqual(response.code, 200)
        data = json.loads(response.body)
        self.assertEqual(data["total"], 1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["status"], "pending")
