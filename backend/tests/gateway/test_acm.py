"""
Интеграционные тесты ACM через Apache Gateway.

Все запросы идут через http://apache_gateway (порт 80) с cookie-аутентификацией.
Apache Lua вызывает ACM перед каждым запросом — тесты проверяют полный стек:
  client → Apache → ACM → (backend-сервис)

Покрывают:
- Ролевой контроль: worker не может обращаться к operator-only эндпоинтам (403)
- Ролевой контроль: operator может обращаться к operator-level эндпоинтам (200)
- Locker-scoped: worker без Допуска → 403 для конкретного шкафа
- Locker-scoped: worker с Допуском → ACM пропускает (не 403)
- Деактивированный пользователь → 403 даже при валидном токене
- Публичные пути → 200/401 без cookie

Важно: locker-scoped тесты проверяют `status_code != 403`, а не `== 200`.
Шкаф с ACM_TEST_LOCKER_UUID не существует в locker_api, поэтому при наличии
Допуска можно получить 404 от locker_api — это корректно, ACM пропустил запрос.
"""
import requests

from conftest import GATEWAY_URL, ACM_TEST_LOCKER_UUID


# =============================================================================
# Ролевой контроль доступа
# =============================================================================

class TestACMRoleBasedAccess:

    def test_worker_denied_users_list(self, worker_session):
        """Worker → GET /api/v1/users → 403 (не 401)."""
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/users")
        assert resp.status_code == 403, f"Expected 403, got {resp.status_code}: {resp.text}"
        data = resp.json()
        assert data["status"] == 403
        assert data["title"] == "Forbidden"

    def test_worker_denied_create_user(self, worker_session):
        """Worker → POST /api/v1/users → 403."""
        session, _ = worker_session
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/users",
            json={"phone": "+79001112233", "full_name": "Should Not Create"},
        )
        assert resp.status_code == 403

    def test_worker_denied_contractors(self, worker_session):
        """Worker → GET /api/v1/contractors → 403."""
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/contractors")
        assert resp.status_code == 403

    def test_worker_denied_create_access_grant(self, worker_session):
        """Worker → POST /api/v1/users/{id}/accesses → 403."""
        session, user_id = worker_session
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/user/{user_id}/accesses",
            json={"locker_ids": [ACM_TEST_LOCKER_UUID]},
        )
        assert resp.status_code == 403

    def test_worker_denied_history(self, worker_session):
        """Worker → GET /api/v1/telemetry → 403."""
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/telemetry/list")
        assert resp.status_code == 403

    def test_worker_allowed_list_lockers(self, worker_session):
        """Worker → GET /api/v1/lockers → ACM пропускает (worker-level), locker_api отвечает.

        Проверяет только пропуск на уровне ACM: в пути коллекции нет UUID,
        поэтому правило ACM ролевое, а строки фильтрует locker_api. Содержимое
        списка (видны ТОЛЬКО шкафы с Допуском) проверяет
        TestACMLockerAccess.test_worker_list_lockers_returns_only_granted.
        """
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/lockers")
        # ACM разрешает worker на GET /lockers; locker_api вернёт 200 (список может быть пустым)
        assert resp.status_code != 403, f"ACM should allow worker to list lockers, got 403"
        assert resp.status_code != 401

    def test_worker_without_grants_sees_empty_list(self, worker_session):
        """Worker без единого Допуска → 200 и ПУСТОЙ список (не весь парк).

        Полный стек на пустой области: ACM шлёт `X-Remote-Lockers:` с пустым
        значением → Lua обязан донести именно "" (а не имя следующего
        заголовка) → locker_api отдаёт ноль строк. Раньше такой worker получал
        весь парк шкафов; на стенде это было 530 шкафов при нуле Допусков.
        """
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/lockers?limit=1000")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        assert body["items"] == [], (
            f"Worker without grants must see no lockers, got {len(body['items'])}"
        )
        assert body["total"] == 0, f"total must be scoped too, got {body['total']}"

    def test_worker_allowed_emergency_requests_get(self, worker_session):
        """Worker → GET /api/v1/emergency-requests → ACM пропускает."""
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/emergency-requests")
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_operator_allowed_users_list(self, operator_session):
        """Operator → GET /api/v1/users → 200."""
        session, _ = operator_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/users")
        assert resp.status_code == 200, f"Operator should access users, got {resp.status_code}: {resp.text}"
        assert isinstance(resp.json()["items"], list)

    def test_operator_allowed_contractors(self, operator_session):
        """Operator → GET /api/v1/contractors → 200."""
        session, _ = operator_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/contractors")
        assert resp.status_code == 200

    def test_operator_allowed_history(self, operator_session):
        """Operator → GET /api/v1/telemetry/list → ACM пропускает."""
        session, _ = operator_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/telemetry/list")
        assert resp.status_code != 403
        assert resp.status_code != 401


# =============================================================================
# Locker-scoped доступ
# =============================================================================

class TestACMLockerAccess:

    def test_worker_denied_specific_locker_without_grant(self, worker_session):
        """Worker без Допуска → GET /lockers/{uuid} → 403."""
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}")
        assert resp.status_code == 403, (
            f"Worker without grant should get 403, got {resp.status_code}"
        )

    def test_worker_allowed_specific_locker_with_grant(self, worker_with_access):
        """Worker с Допуском → GET /lockers/{uuid} → ACM пропускает (не 403)."""
        session, _, locker_uuid = worker_with_access
        resp = session.get(f"{GATEWAY_URL}/api/v1/locker/{locker_uuid}")
        assert resp.status_code != 403, (
            f"Worker with grant should pass ACM, but got 403"
        )
        assert resp.status_code != 401
        # Ожидаем 404 от locker_api (шкаф не существует) — это нормально

    def test_worker_denied_commands_without_grant(self, worker_session):
        """Worker без Допуска → POST /lockers/{uuid}/commands → 403."""
        session, _ = worker_session
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/commands",
            json={"command": "open"},
        )
        assert resp.status_code == 403

    def test_worker_allowed_commands_with_grant(self, worker_with_access):
        """Worker с Допуском → POST /lockers/{uuid}/commands → ACM пропускает (не 403)."""
        session, _, locker_uuid = worker_with_access
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/locker/{locker_uuid}/commands",
            json={"command": "open"},
        )
        assert resp.status_code != 403, (
            f"Worker with grant should pass ACM for commands, but got 403"
        )
        assert resp.status_code != 401

    def test_worker_list_lockers_returns_only_granted(self, worker_with_scoped_lockers):
        """Worker → GET /api/v1/lockers → в списке ТОЛЬКО шкафы с Допуском.

        Полный стек: ACM отдаёт X-Remote-Lockers со списком Допусков → Lua
        прокидывает заголовок → locker_api резолвит его в область видимости и
        фильтрует строки в SQL. Раньше worker получал ВЕСЬ парк шкафов.

        limit=1000 (максимум ручки) намеренно: с дефолтным limit=100 шкаф без
        Допуска мог бы не попасть на первую страницу, и тест зеленел бы даже
        при сломанной фильтрации.
        """
        session, _, granted_id, ungranted_id = worker_with_scoped_lockers
        resp = session.get(f"{GATEWAY_URL}/api/v1/lockers?limit=1000")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

        body = resp.json()
        returned_ids = {item["id"] for item in body["items"]}

        # Точная принадлежность, а не счётчик: другие тесты могли оставить шкафы.
        assert granted_id in returned_ids, (
            f"Granted locker {granted_id} must be visible, got {sorted(returned_ids)}"
        )
        assert ungranted_id not in returned_ids, (
            f"Locker without grant {ungranted_id} leaked into worker's list: "
            f"{sorted(returned_ids)}"
        )
        # total обязан считаться по той же области, иначе пагинация обещает
        # страницы, которых worker не увидит.
        assert body["total"] == len(returned_ids), (
            f"total={body['total']} diverged from items={len(returned_ids)}"
        )

    def test_operator_allowed_any_locker(self, operator_session):
        """Operator → GET /lockers/{uuid} → ACM пропускает без Допуска."""
        session, _ = operator_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}")
        # Оператор не ограничен Допусками — ACM пропускает
        assert resp.status_code != 403
        assert resp.status_code != 401


# =============================================================================
# SEC-02: обход grant-чека через не-канонический вид UUID
# Worker БЕЗ Допуска не должен получить доступ к шкафу, передав 32-символьный
# dashless UUID — раньше узкий паттерн не совпадал → ACM пропускал (allow).
# =============================================================================

class TestACMNonCanonicalUuidBypass:
    # dashless-форма ACM_TEST_LOCKER_UUID (cccccccc-cccc-...): Postgres uuid
    # принимает её и резолвит в тот же шкаф, что и дефисный вид.
    DASHLESS_LOCKER = "cccccccccccccccccccccccccccccccc"

    def test_worker_denied_commands_dashless_without_grant(self, worker_session):
        """Worker без Допуска → POST /locker/{dashless}/commands → 403 (был обход)."""
        session, _ = worker_session
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/locker/{self.DASHLESS_LOCKER}/commands",
            json={"type": "open"},
        )
        assert resp.status_code == 403, (
            f"Dashless UUID must NOT bypass grant check, got {resp.status_code}: {resp.text}"
        )

    def test_worker_denied_get_locker_dashless_without_grant(self, worker_session):
        """Worker без Допуска → GET /locker/{dashless} → 403 (IDOR-вариант обхода)."""
        session, _ = worker_session
        resp = session.get(
            f"{GATEWAY_URL}/api/v1/locker/{self.DASHLESS_LOCKER}"
        )
        assert resp.status_code == 403, (
            f"Dashless UUID must NOT bypass IDOR read, got {resp.status_code}: {resp.text}"
        )

    def test_worker_denied_garbage_locker_segment(self, worker_session):
        """Непарсимый сегмент locker-scoped пути → 403 (deny, не skip)."""
        session, _ = worker_session
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/locker/not-a-uuid/commands",
            json={"type": "open"},
        )
        assert resp.status_code == 403


# =============================================================================
# Деактивированный пользователь
# =============================================================================

class TestACMDeactivatedUser:

    def test_deactivated_user_denied(self, worker_session, admin_session):
        """
        После деактивации пользователя через admin-сессию,
        его валидный токен должен получать 403 от ACM.
        """
        session, user_id = worker_session

        # Убеждаемся, что до деактивации токен работает
        resp_before = session.get(f"{GATEWAY_URL}/api/v1/lockers")
        assert resp_before.status_code != 401, "Token should be valid before deactivation"

        # Деактивируем пользователя через admin-сессию
        deactivate_resp = admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}")
        assert deactivate_resp.status_code == 204, (
            f"Failed to deactivate user: {deactivate_resp.text}"
        )

        # Следующий запрос должен получить 403 от ACM (токен валиден, но пользователь деактивирован)
        resp_after = session.get(f"{GATEWAY_URL}/api/v1/lockers")
        assert resp_after.status_code == 403, (
            f"Deactivated user should get 403, got {resp_after.status_code}"
        )
        data = resp_after.json()
        assert data["status"] == 403


# =============================================================================
# Публичные пути (без cookie)
# =============================================================================

class TestACMPublicPaths:

    def test_auth_login_accessible_without_cookie(self, wait_for_gateway):
        """POST /api/v1/auth/login без cookie → не 401 (публичный путь)."""
        resp = requests.post(
            f"{GATEWAY_URL}/api/v1/auth/login",
            json={"phone": "+70000000000", "password": "x"},
        )
        # ACM пропускает /api/v1/auth/*, auth_api вернёт 401 (неверные данные), но не от ACM
        assert resp.status_code != 403
        # Ожидаем 401 от auth_api (неверный пароль), а не от ACM (нет cookie)
        assert resp.status_code in (200, 400, 401)

    def test_auth_verify_accessible_without_cookie(self, wait_for_gateway):
        """POST /api/v1/auth/verify без cookie → не 401 от ACM."""
        resp = requests.post(
            f"{GATEWAY_URL}/api/v1/auth/verify",
            json={"phone": "+70000000000", "code": "000000"},
        )
        assert resp.status_code != 403
        assert resp.status_code in (200, 400, 401)

    def test_protected_endpoint_without_cookie_returns_401(self, wait_for_gateway):
        """GET /api/v1/users без cookie → 401 от ACM (нет токена)."""
        resp = requests.get(f"{GATEWAY_URL}/api/v1/users")
        assert resp.status_code == 401


# =============================================================================
# PUT /contractor/{id} — ролевой доступ
# =============================================================================

class TestACMPutContractor:
    """PUT /api/v1/contractor/{id} требует роль OPERATOR или выше."""

    def test_admin_allowed_put_contractor(self, admin_session, created_contractor):
        """Admin → PUT /api/v1/contractor/{id} → ACM пропускает (не 403)."""
        contractor_id = created_contractor["id"]
        resp = admin_session.put(
            f"{GATEWAY_URL}/api/v1/contractor/{contractor_id}",
            json={"name": "ООО Обновлённый Подрядчик (admin)"},
        )
        assert resp.status_code != 403, (
            f"Admin should be allowed to PUT contractor, got 403"
        )
        assert resp.status_code != 401

    def test_operator_allowed_put_contractor(self, operator_session, created_contractor):
        """Operator → PUT /api/v1/contractor/{id} → ACM пропускает (не 403)."""
        session, _ = operator_session
        contractor_id = created_contractor["id"]
        resp = session.put(
            f"{GATEWAY_URL}/api/v1/contractor/{contractor_id}",
            json={"name": "ООО Обновлённый Подрядчик (operator)"},
        )
        assert resp.status_code != 403, (
            f"Operator should be allowed to PUT contractor, got 403"
        )
        assert resp.status_code != 401

    def test_worker_denied_put_contractor(self, worker_session, created_contractor):
        """Worker → PUT /api/v1/contractor/{id} → 403."""
        session, _ = worker_session
        contractor_id = created_contractor["id"]
        resp = session.put(
            f"{GATEWAY_URL}/api/v1/contractor/{contractor_id}",
            json={"name": "ООО Не Должно Обновиться"},
        )
        assert resp.status_code == 403, (
            f"Worker should be denied PUT contractor, got {resp.status_code}"
        )
        data = resp.json()
        assert data["status"] == 403
        assert data["title"] == "Forbidden"


# =============================================================================
# Реактивация пользователя и допуска
# =============================================================================

class TestACMReactivate:
    """POST /user/{id}/reactivate и POST /access/{id}/reactivate требуют OPERATOR."""

    def test_worker_denied_reactivate_user(self, worker_session, created_user):
        """Worker → POST /api/v1/user/{id}/reactivate → 403."""
        session, _ = worker_session
        user_id = created_user["id"]
        resp = session.post(f"{GATEWAY_URL}/api/v1/user/{user_id}/reactivate")
        assert resp.status_code == 403, (
            f"Worker should be denied reactivate user, got {resp.status_code}"
        )
        data = resp.json()
        assert data["status"] == 403
        assert data["title"] == "Forbidden"

    def test_operator_allowed_reactivate_user(self, operator_session, admin_session, created_user):
        """Operator → POST /api/v1/user/{id}/reactivate → ACM пропускает (не 403)."""
        session, _ = operator_session
        user_id = created_user["id"]
        # Деактивируем через admin, чтобы реактивация имела смысл
        admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}")
        resp = session.post(f"{GATEWAY_URL}/api/v1/user/{user_id}/reactivate")
        assert resp.status_code != 403, (
            f"Operator should pass ACM for reactivate user, got 403"
        )
        assert resp.status_code != 401

    def test_worker_denied_reactivate_access(self, worker_session):
        """Worker → POST /api/v1/access/{id}/reactivate → 403."""
        session, _ = worker_session
        resp = session.post(f"{GATEWAY_URL}/api/v1/access/1/reactivate")
        assert resp.status_code == 403, (
            f"Worker should be denied reactivate access, got {resp.status_code}"
        )
        data = resp.json()
        assert data["status"] == 403
        assert data["title"] == "Forbidden"

    def test_operator_allowed_reactivate_access(self, operator_session):
        """Operator → POST /api/v1/access/{id}/reactivate → ACM пропускает (не 403).

        Используем id=1 — если не существует, backend вернёт 404, но не 403.
        """
        session, _ = operator_session
        resp = session.post(f"{GATEWAY_URL}/api/v1/access/1/reactivate")
        assert resp.status_code != 403, (
            f"Operator should pass ACM for reactivate access, got 403"
        )
        assert resp.status_code != 401

    def test_admin_allowed_reactivate_user(self, admin_session, created_user):
        """Admin → POST /api/v1/user/{id}/reactivate → ACM пропускает (не 403)."""
        user_id = created_user["id"]
        # Деактивируем сначала, чтобы реактивация имела смысл
        admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}")
        resp = admin_session.post(f"{GATEWAY_URL}/api/v1/user/{user_id}/reactivate")
        assert resp.status_code != 403, (
            f"Admin should pass ACM for reactivate user, got 403"
        )
        assert resp.status_code != 401

    def test_admin_allowed_reactivate_access(self, admin_session):
        """Admin → POST /api/v1/access/{id}/reactivate → ACM пропускает (не 403)."""
        resp = admin_session.post(f"{GATEWAY_URL}/api/v1/access/1/reactivate")
        assert resp.status_code != 403, (
            f"Admin should pass ACM for reactivate access, got 403"
        )
        assert resp.status_code != 401


# =============================================================================
# Write-пути history (аудит стенда 2026-06-11)
# =============================================================================

class TestACMSecurityEventResolve:
    """PATCH /api/v1/events/security/{id} — OPERATOR+; раньше default-deny."""

    def test_worker_denied_resolve_event(self, worker_session):
        session, _ = worker_session
        resp = session.patch(
            f"{GATEWAY_URL}/api/v1/events/security/1",
            json={"resolved_by": 1},
        )
        assert resp.status_code == 403

    def test_operator_allowed_resolve_event(self, operator_session):
        """ACM пропускает; history_api ответит 404 (событие не существует) — не 403."""
        session, user_id = operator_session
        resp = session.patch(
            f"{GATEWAY_URL}/api/v1/events/security/999999999",
            json={"resolved_by": user_id},
        )
        assert resp.status_code != 403, f"ACM must allow operator, got {resp.status_code}: {resp.text}"
        assert resp.status_code != 401

    def test_admin_allowed_resolve_event(self, admin_session):
        resp = admin_session.patch(
            f"{GATEWAY_URL}/api/v1/events/security/999999999",
            json={"resolved_by": 1},
        )
        assert resp.status_code != 403
        assert resp.status_code != 401


class TestACMHistoryExternalIngest:
    """Внешние ingest-POST (дубли /internal/*) — только администратор."""

    def test_operator_denied_post_security_event(self, operator_session):
        session, _ = operator_session
        resp = session.post(f"{GATEWAY_URL}/api/v1/events/security", json={})
        assert resp.status_code == 403

    def test_operator_denied_post_telemetry(self, operator_session):
        session, _ = operator_session
        resp = session.post(f"{GATEWAY_URL}/api/v1/telemetry", json={})
        assert resp.status_code == 403

    def test_operator_denied_post_command_history(self, operator_session):
        session, _ = operator_session
        resp = session.post(f"{GATEWAY_URL}/api/v1/commands/history", json={})
        assert resp.status_code == 403

    def test_admin_allowed_post_security_event(self, admin_session):
        """ACM пропускает; history_api вернёт 400 на пустое тело — не 403."""
        resp = admin_session.post(f"{GATEWAY_URL}/api/v1/events/security", json={})
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_admin_allowed_post_telemetry(self, admin_session):
        """ACM пропускает админский ingest; history_api валидирует тело — не 403."""
        resp = admin_session.post(f"{GATEWAY_URL}/api/v1/telemetry", json={})
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_admin_allowed_post_command_history(self, admin_session):
        resp = admin_session.post(f"{GATEWAY_URL}/api/v1/commands/history", json={})
        assert resp.status_code != 403
        assert resp.status_code != 401


# =============================================================================
# Singular-маршруты user_api
# =============================================================================

class TestACMContractorDetail:
    """GET /api/v1/contractor/{id} — OPERATOR+; раньше default-deny."""

    def test_worker_denied_get_contractor(self, worker_session):
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/contractor/1")
        assert resp.status_code == 403

    def test_operator_allowed_get_contractor(self, operator_session, created_contractor):
        session, _ = operator_session
        contractor_id = created_contractor["id"]
        resp = session.get(f"{GATEWAY_URL}/api/v1/contractor/{contractor_id}")
        assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"


class TestACMEmergencyRequestDetail:
    """Singular /api/v1/emergency-request/{id}: раньше ни правила, ни ProxyPass."""

    def test_worker_allowed_get_request(self, worker_session):
        """ACM пропускает worker; user_api вернёт 404 (заявки нет) — не 403 и не SPA."""
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/emergency-request/999999999")
        assert resp.status_code == 404, (
            f"Expected 404 from user_api (proxied + allowed), got {resp.status_code}: {resp.text}"
        )

    def test_worker_denied_close_request(self, worker_session):
        session, _ = worker_session
        resp = session.delete(f"{GATEWAY_URL}/api/v1/emergency-request/999999999")
        assert resp.status_code == 403

    def test_operator_allowed_close_request(self, operator_session):
        """ACM пропускает; user_api вернёт 404 — не 403 и не SPA-страница."""
        session, _ = operator_session
        resp = session.delete(f"{GATEWAY_URL}/api/v1/emergency-request/999999999")
        assert resp.status_code == 404, (
            f"Expected 404 from user_api, got {resp.status_code}: {resp.text}"
        )

    def test_worker_denied_request_logs(self, worker_session):
        session, _ = worker_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/emergency-request/999999999/logs")
        assert resp.status_code == 403

    def test_operator_allowed_request_logs(self, operator_session):
        session, _ = operator_session
        resp = session.get(f"{GATEWAY_URL}/api/v1/emergency-request/999999999/logs")
        assert resp.status_code == 404


# =============================================================================
# Sensor-types детальные операции
# =============================================================================

class TestACMSensorTypeDetail:
    """PUT/DELETE /api/v1/sensor-types/{id} — ADMINISTRATOR; раньше default-deny."""

    def test_operator_denied_put_sensor_type(self, operator_session):
        session, _ = operator_session
        resp = session.put(
            f"{GATEWAY_URL}/api/v1/sensor-types/999999",
            json={"name": "x"},
        )
        assert resp.status_code == 403

    def test_admin_allowed_put_sensor_type(self, admin_session):
        """ACM пропускает; locker_api ответит 404/400 — не 403."""
        resp = admin_session.put(
            f"{GATEWAY_URL}/api/v1/sensor-types/999999",
            json={"name": "x"},
        )
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_operator_denied_delete_sensor_type(self, operator_session):
        session, _ = operator_session
        resp = session.delete(f"{GATEWAY_URL}/api/v1/sensor-types/999999")
        assert resp.status_code == 403

    def test_admin_allowed_delete_sensor_type(self, admin_session):
        resp = admin_session.delete(f"{GATEWAY_URL}/api/v1/sensor-types/999999")
        assert resp.status_code != 403
        assert resp.status_code != 401


# =============================================================================
# Отзыв токенов администратором
# =============================================================================

class TestACMTokenRevoke:
    """DELETE /api/v1/auth/users/{id}/tokens — раньше публичное правило
    перехватывало путь, Lua не передавал identity, handler всегда 401."""

    def test_worker_denied_token_revoke(self, worker_session):
        session, _ = worker_session
        resp = session.delete(f"{GATEWAY_URL}/api/v1/auth/users/1/tokens")
        assert resp.status_code == 403

    def test_operator_denied_token_revoke(self, operator_session):
        session, _ = operator_session
        resp = session.delete(f"{GATEWAY_URL}/api/v1/auth/users/1/tokens")
        assert resp.status_code == 403

    def test_admin_can_revoke_tokens(self, admin_session, created_user):
        """Полный путь: ACM(admin) → Lua identity → auth_api 200 {revoked: N}."""
        user_id = created_user["id"]
        resp = admin_session.delete(f"{GATEWAY_URL}/api/v1/auth/users/{user_id}/tokens")
        assert resp.status_code == 200, (
            f"Expected 200 from auth_api, got {resp.status_code}: {resp.text}"
        )
        assert "revoked" in resp.json()

    def test_no_cookie_token_revoke_unauthorized(self, wait_for_gateway):
        """Без cookie — 401 от ACM (путь больше не публичный)."""
        resp = requests.delete(f"{GATEWAY_URL}/api/v1/auth/users/1/tokens")
        assert resp.status_code == 401

    def test_trailing_slash_variant_not_public(self, operator_session):
        """Вариант пути с trailing slash не должен проваливаться в публичное правило."""
        session, _ = operator_session
        resp = session.delete(f"{GATEWAY_URL}/api/v1/auth/users/1/tokens/")
        assert resp.status_code == 403


# =============================================================================
# Расшейдованные правила под-ресурсов шкафа
# =============================================================================

class TestACMLockerSubresourceRoles:
    """После реордеринга специфичные правила sensors/thresholds работают."""

    def test_worker_denied_locker_sensors(self, worker_with_access):
        """GET sensors теперь OPERATOR (раньше затенялось worker-правилом)."""
        session, _, locker_uuid = worker_with_access
        resp = session.get(f"{GATEWAY_URL}/api/v1/locker/{locker_uuid}/sensors")
        assert resp.status_code == 403

    def test_operator_allowed_locker_sensors(self, operator_session):
        session, _ = operator_session
        resp = session.get(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors"
        )
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_operator_allowed_delete_sensor(self, operator_session):
        """DELETE sensors теперь OPERATOR (раньше затенялось admin-правилом)."""
        session, _ = operator_session
        resp = session.delete(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors/00000000-0000-0000-0000-000000000000"
        )
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_operator_denied_create_threshold(self, operator_session):
        """POST thresholds теперь ADMINISTRATOR (раньше затенялось operator-правилом)."""
        session, _ = operator_session
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors/00000000-0000-0000-0000-000000000000/thresholds",
            json={},
        )
        assert resp.status_code == 403

    def test_admin_allowed_create_threshold(self, admin_session):
        resp = admin_session.post(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors/00000000-0000-0000-0000-000000000000/thresholds",
            json={},
        )
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_operator_denied_delete_threshold_detail(self, operator_session):
        """DELETE threshold detail — ADMINISTRATOR (ослабление поймано на ревью)."""
        session, _ = operator_session
        resp = session.delete(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors/00000000-0000-0000-0000-000000000000/thresholds/1"
        )
        assert resp.status_code == 403

    def test_admin_allowed_delete_threshold_detail(self, admin_session):
        resp = admin_session.delete(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors/00000000-0000-0000-0000-000000000000/thresholds/1"
        )
        assert resp.status_code != 403
        assert resp.status_code != 401

    def test_operator_denied_put_threshold_detail(self, operator_session):
        """PUT threshold detail — ADMINISTRATOR (отдельное правило, не DELETE)."""
        session, _ = operator_session
        resp = session.put(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors/00000000-0000-0000-0000-000000000000/thresholds/1",
            json={},
        )
        assert resp.status_code == 403

    def test_admin_allowed_put_threshold_detail(self, admin_session):
        resp = admin_session.put(
            f"{GATEWAY_URL}/api/v1/locker/{ACM_TEST_LOCKER_UUID}/sensors/00000000-0000-0000-0000-000000000000/thresholds/1",
            json={},
        )
        assert resp.status_code != 403
        assert resp.status_code != 401
