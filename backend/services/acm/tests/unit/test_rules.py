"""
Unit-тесты для app.rules — матрица разрешений ACM.

Покрывают:
- Публичные эндпоинты (без токена)
- Роль-уровневые ограничения (worker/operator/administrator)
- Locker-scoped проверка Допуска для worker
- Вспомогательные функции find_rule, is_locker_scoped, check_access
- Иерархию ролей через Role enum
"""
import pytest

from app.rules import (
    Role,
    Rule,
    check_access,
    find_rule,
    is_locker_scoped,
)

LOCKER_UUID = "550e8400-e29b-41d4-a716-446655440000"
OTHER_UUID = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"


# ---------------------------------------------------------------------------
# Role enum
# ---------------------------------------------------------------------------

class TestRole:
    def test_hierarchy_order(self):
        assert Role.WORKER < Role.OPERATOR < Role.ADMINISTRATOR

    def test_from_str_valid(self):
        assert Role.from_str("worker") == Role.WORKER
        assert Role.from_str("operator") == Role.OPERATOR
        assert Role.from_str("administrator") == Role.ADMINISTRATOR

    def test_from_str_case_insensitive(self):
        assert Role.from_str("WORKER") == Role.WORKER
        assert Role.from_str("Operator") == Role.OPERATOR

    def test_from_str_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown role"):
            Role.from_str("superadmin")

    def test_allows_hierarchy(self):
        rule_operator = Rule("GET", r".*", Role.OPERATOR)
        assert rule_operator.allows(Role.ADMINISTRATOR)
        assert rule_operator.allows(Role.OPERATOR)
        assert not rule_operator.allows(Role.WORKER)

    def test_public_rule_always_allowed(self):
        rule = Rule("POST", r"^/api/v1/auth/", None)
        assert rule.allows(Role.WORKER)
        assert rule.allows(Role.OPERATOR)


# ---------------------------------------------------------------------------
# Rule.matches
# ---------------------------------------------------------------------------

class TestRuleMatches:
    def test_exact_method_match(self):
        rule = Rule("GET", r"^/api/v1/users$", Role.OPERATOR)
        assert rule.matches("GET", "/api/v1/users")
        assert not rule.matches("POST", "/api/v1/users")

    def test_wildcard_method(self):
        rule = Rule("*", r"^/api/v1/auth/", None)
        assert rule.matches("GET", "/api/v1/auth/login")
        assert rule.matches("POST", "/api/v1/auth/login")
        assert rule.matches("OPTIONS", "/api/v1/auth/verify")

    def test_method_case_insensitive(self):
        rule = Rule("get", r"^/api/v1/users$", Role.OPERATOR)
        assert rule.matches("GET", "/api/v1/users")

    def test_path_regex(self):
        rule = Rule("GET", r"^/api/v1/locker/[^/]+/commands$", Role.WORKER)
        assert rule.matches("GET", f"/api/v1/locker/{LOCKER_UUID}/commands")
        assert not rule.matches("GET", "/api/v1/lockers")


# ---------------------------------------------------------------------------
# find_rule
# ---------------------------------------------------------------------------

_FIND_RULE_CASES = [
    # (method, path, expected_min_role) — None означает публичный эндпоинт
    ("POST", "/api/v1/auth/login", None),
    ("GET", "/health", None),
    ("GET", "/api/v1/users", Role.OPERATOR),
    ("GET", "/api/v1/lockers", Role.WORKER),
    ("POST", f"/api/v1/locker/{LOCKER_UUID}/commands", Role.WORKER),
    ("POST", "/api/v1/lockers", Role.OPERATOR),
    ("DELETE", f"/api/v1/locker/{LOCKER_UUID}", Role.ADMINISTRATOR),
    ("POST", "/api/v1/emergency-requests", Role.WORKER),
    ("POST", "/api/v1/contractors", Role.OPERATOR),
    ("GET", "/api/v1/telemetry", Role.OPERATOR),
    ("GET", "/api/v1/notifications", Role.WORKER),
    ("POST", "/api/v1/notifications", Role.OPERATOR),
    ("POST", "/api/v1/reports", Role.OPERATOR),
    ("GET", "/api/v1/reports", Role.OPERATOR),
    ("GET", f"/api/v1/report/{LOCKER_UUID}", Role.OPERATOR),
    ("PATCH", f"/api/v1/report/{LOCKER_UUID}", Role.OPERATOR),
    ("DELETE", f"/api/v1/report/{LOCKER_UUID}", Role.OPERATOR),
    # --- write-пути history (аудит стенда 2026-06-11: PATCH 2846 падал в default-deny) ---
    ("PATCH", "/api/v1/events/security/2846", Role.OPERATOR),
    ("POST", "/api/v1/events/security", Role.ADMINISTRATOR),
    ("POST", "/api/v1/telemetry", Role.ADMINISTRATOR),
    ("POST", "/api/v1/commands/history", Role.ADMINISTRATOR),
    # --- singular-маршруты user_api ---
    ("GET", "/api/v1/contractor/7", Role.OPERATOR),
    ("GET", "/api/v1/emergency-request/5", Role.WORKER),
    ("DELETE", "/api/v1/emergency-request/5", Role.OPERATOR),
    ("GET", "/api/v1/emergency-request/5/logs", Role.OPERATOR),
    # --- sensor-types детальные (integer id) ---
    ("PUT", "/api/v1/sensor-types/3", Role.ADMINISTRATOR),
    ("DELETE", "/api/v1/sensor-types/3", Role.ADMINISTRATOR),
    # --- отзыв токенов: правило ДО публичного ^/api/v1/auth/; вся ветка /auth/users/ ---
    ("DELETE", "/api/v1/auth/users/7/tokens", Role.ADMINISTRATOR),
    ("DELETE", "/api/v1/auth/users/7/tokens/", Role.ADMINISTRATOR),  # trailing slash
    ("GET", "/api/v1/auth/users/7/tokens", Role.ADMINISTRATOR),  # GET тоже не публичный
    # --- затенение специфичных правил шкафа широкими (first-match) ---
    ("GET", f"/api/v1/locker/{LOCKER_UUID}/sensors", Role.OPERATOR),
    ("DELETE", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc", Role.OPERATOR),
    ("GET", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds", Role.OPERATOR),
    ("POST", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds", Role.ADMINISTRATOR),
    # --- пороги: PUT/DELETE только администратору (как и POST) ---
    ("PUT", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds/5", Role.ADMINISTRATOR),
    ("DELETE", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds/5", Role.ADMINISTRATOR),
    ("GET", f"/api/v1/locker/{LOCKER_UUID}/relays", Role.WORKER),
    ("PATCH", f"/api/v1/locker/{LOCKER_UUID}/relays/2", Role.ADMINISTRATOR),
    ("PATCH", f"/api/v1/locker/{LOCKER_UUID}/relays/9", Role.ADMINISTRATOR),
]


class TestFindRule:
    @pytest.mark.parametrize("method,path,expected_role", _FIND_RULE_CASES)
    def test_rule_matches_expected_role(self, method, path, expected_role):
        rule = find_rule(method, path)
        assert rule is not None, f"No rule found for {method} {path}"
        assert rule.min_role == expected_role, (
            f"{method} {path}: expected min_role={expected_role}, got {rule.min_role}"
        )

    def test_unknown_path_returns_none(self):
        assert find_rule("GET", "/api/v1/unknown-endpoint") is None


# ---------------------------------------------------------------------------
# is_locker_scoped
# ---------------------------------------------------------------------------

LOCKER_UUID_UPPER = "550E8400-E29B-41D4-A716-446655440000"


class TestIsLockerScoped:
    def test_specific_locker(self):
        result = is_locker_scoped(f"/api/v1/locker/{LOCKER_UUID}")
        assert result == LOCKER_UUID

    def test_locker_commands(self):
        result = is_locker_scoped(f"/api/v1/locker/{LOCKER_UUID}/commands")
        assert result == LOCKER_UUID

    def test_locker_sensors(self):
        result = is_locker_scoped(f"/api/v1/locker/{LOCKER_UUID}/sensors")
        assert result == LOCKER_UUID

    def test_lockers_list_is_not_scoped(self):
        # Коллекция намеренно НЕ locker-scoped на уровне ACM: в пути нет
        # UUID, а построчную фильтрацию по Допуску выполняет locker_api
        # (через X-Remote-Lockers), т.к. ACM читает scud_users и не видит
        # строк scud_lockers.
        assert is_locker_scoped("/api/v1/lockers") is None

    def test_non_locker_path_is_not_scoped(self):
        assert is_locker_scoped("/api/v1/users") is None

    def test_uppercase_uuid_returns_lowercase(self):
        """UUID в верхнем регистре должен матчиться и возвращаться в нижнем."""
        result = is_locker_scoped(f"/api/v1/locker/{LOCKER_UUID_UPPER}")
        assert result == LOCKER_UUID  # нормализован до lowercase

    def test_uppercase_uuid_commands_returns_lowercase(self):
        result = is_locker_scoped(f"/api/v1/locker/{LOCKER_UUID_UPPER}/commands")
        assert result == LOCKER_UUID


# ---------------------------------------------------------------------------
# check_access — полная функция
# ---------------------------------------------------------------------------

class TestCheckAccess:
    # --- Публичные эндпоинты ---

    def test_public_auth_always_allowed(self):
        allowed, reason = check_access("POST", "/api/v1/auth/login", Role.WORKER, [], False)
        assert allowed
        assert reason == ""

    def test_public_health_always_allowed(self):
        allowed, reason = check_access("GET", "/health", Role.WORKER, [], False)
        assert allowed

    # --- Роль-уровневые проверки ---

    def test_worker_cannot_list_users(self):
        allowed, reason = check_access("GET", "/api/v1/users", Role.WORKER, [], False)
        assert not allowed
        assert "worker" in reason

    def test_operator_can_list_users(self):
        allowed, reason = check_access("GET", "/api/v1/users", Role.OPERATOR, [], False)
        assert allowed

    def test_administrator_can_list_users(self):
        allowed, reason = check_access("GET", "/api/v1/users", Role.ADMINISTRATOR, [], False)
        assert allowed

    def test_worker_cannot_create_contractor(self):
        allowed, _ = check_access("POST", "/api/v1/contractors", Role.WORKER, [], False)
        assert not allowed

    def test_operator_can_create_contractor(self):
        allowed, _ = check_access("POST", "/api/v1/contractors", Role.OPERATOR, [], False)
        assert allowed

    def test_worker_cannot_delete_locker(self):
        allowed, _ = check_access("DELETE", f"/api/v1/locker/{LOCKER_UUID}", Role.WORKER, [LOCKER_UUID], False)
        assert not allowed

    def test_operator_cannot_delete_locker(self):
        allowed, _ = check_access("DELETE", f"/api/v1/locker/{LOCKER_UUID}", Role.OPERATOR, [], False)
        assert not allowed

    def test_administrator_can_delete_locker(self):
        allowed, _ = check_access("DELETE", f"/api/v1/locker/{LOCKER_UUID}", Role.ADMINISTRATOR, [], False)
        assert allowed

    def test_unknown_path_denied(self):
        allowed, reason = check_access("GET", "/api/v1/nonexistent", Role.ADMINISTRATOR, [], False)
        assert not allowed
        assert "No matching rule" in reason

    # --- Locker-scoped Допуски для worker ---

    def test_worker_with_access_can_get_locker(self):
        allowed, _ = check_access(
            "GET", f"/api/v1/locker/{LOCKER_UUID}",
            Role.WORKER, [LOCKER_UUID], False,
        )
        assert allowed

    def test_worker_without_access_denied_specific_locker(self):
        allowed, reason = check_access(
            "GET", f"/api/v1/locker/{LOCKER_UUID}",
            Role.WORKER, [OTHER_UUID], False,
        )
        assert not allowed
        assert LOCKER_UUID in reason

    def test_worker_with_full_access_can_get_any_locker(self):
        allowed, _ = check_access(
            "GET", f"/api/v1/locker/{LOCKER_UUID}",
            Role.WORKER, [], True,
        )
        assert allowed

    def test_worker_can_post_command_with_access(self):
        allowed, _ = check_access(
            "POST", f"/api/v1/locker/{LOCKER_UUID}/commands",
            Role.WORKER, [LOCKER_UUID], False,
        )
        assert allowed

    def test_worker_cannot_post_command_without_access(self):
        allowed, _ = check_access(
            "POST", f"/api/v1/locker/{LOCKER_UUID}/commands",
            Role.WORKER, [], False,
        )
        assert not allowed

    def test_worker_can_confirm_command_with_access(self):
        """Step-up confirm — worker с Допуском на тот же шкаф."""
        allowed, _ = check_access(
            "POST", f"/api/v1/locker/{LOCKER_UUID}/commands/confirm",
            Role.WORKER, [LOCKER_UUID], False,
        )
        assert allowed

    def test_operator_can_get_any_locker_without_access_record(self):
        # Оператор не проходит locker-scoped проверку
        allowed, _ = check_access(
            "GET", f"/api/v1/locker/{LOCKER_UUID}",
            Role.OPERATOR, [], False,
        )
        assert allowed

    def test_worker_can_list_lockers_role_level(self):
        # GET /api/v1/lockers — не locker-scoped (нет UUID), worker проходит по роли.
        # Это намеренно: построчная фильтрация по Допуску для worker'а происходит
        # ниже по цепочке, в locker_api (по X-Remote-Lockers), а не здесь.
        allowed, _ = check_access("GET", "/api/v1/lockers", Role.WORKER, [], False)
        assert allowed

    def test_worker_can_access_locker_from_multiple_grants(self):
        allowed, _ = check_access(
            "GET", f"/api/v1/locker/{LOCKER_UUID}",
            Role.WORKER, [OTHER_UUID, LOCKER_UUID], False,
        )
        assert allowed

    # --- Допуски ---

    def test_worker_cannot_create_access(self):
        allowed, _ = check_access("POST", "/api/v1/user/1/accesses", Role.WORKER, [], False)
        assert not allowed

    def test_operator_can_create_access(self):
        allowed, _ = check_access("POST", "/api/v1/user/1/accesses", Role.OPERATOR, [], False)
        assert allowed

    def test_operator_can_patch_access(self):
        allowed, _ = check_access("PATCH", "/api/v1/access/5", Role.OPERATOR, [], False)
        assert allowed

    def test_worker_cannot_patch_access(self):
        allowed, _ = check_access("PATCH", "/api/v1/access/5", Role.WORKER, [], False)
        assert not allowed

    # --- Уведомления ---

    def test_worker_can_list_notifications(self):
        allowed, _ = check_access("GET", "/api/v1/notifications", Role.WORKER, [], False)
        assert allowed

    def test_worker_cannot_create_notification(self):
        allowed, _ = check_access("POST", "/api/v1/notifications", Role.WORKER, [], False)
        assert not allowed

    def test_operator_can_create_notification(self):
        allowed, _ = check_access("POST", "/api/v1/notifications", Role.OPERATOR, [], False)
        assert allowed

    def test_worker_can_get_notification(self):
        allowed, _ = check_access(
            "GET", "/api/v1/notification/550e8400-e29b-41d4-a716-446655440000",
            Role.WORKER, [], False,
        )
        assert allowed

    def test_worker_cannot_cancel_notification(self):
        allowed, _ = check_access(
            "DELETE", "/api/v1/notification/550e8400-e29b-41d4-a716-446655440000",
            Role.WORKER, [], False,
        )
        assert not allowed

    def test_operator_can_cancel_notification(self):
        allowed, _ = check_access(
            "DELETE", "/api/v1/notification/550e8400-e29b-41d4-a716-446655440000",
            Role.OPERATOR, [], False,
        )
        assert allowed

    # --- Отчёты ---

    def test_worker_cannot_create_report(self):
        allowed, _ = check_access("POST", "/api/v1/reports", Role.WORKER, [], False)
        assert not allowed

    def test_operator_can_create_report(self):
        allowed, _ = check_access("POST", "/api/v1/reports", Role.OPERATOR, [], False)
        assert allowed

    def test_worker_cannot_list_reports(self):
        allowed, _ = check_access("GET", "/api/v1/reports", Role.WORKER, [], False)
        assert not allowed

    def test_operator_can_list_reports(self):
        allowed, _ = check_access("GET", "/api/v1/reports", Role.OPERATOR, [], False)
        assert allowed

    def test_worker_cannot_get_report(self):
        allowed, _ = check_access(
            "GET", f"/api/v1/report/{LOCKER_UUID}", Role.WORKER, [], False,
        )
        assert not allowed

    def test_operator_can_get_report(self):
        allowed, _ = check_access(
            "GET", f"/api/v1/report/{LOCKER_UUID}", Role.OPERATOR, [], False,
        )
        assert allowed

    def test_worker_cannot_download_report(self):
        allowed, _ = check_access(
            "GET", f"/api/v1/report/{LOCKER_UUID}/download", Role.WORKER, [], False,
        )
        assert not allowed

    def test_operator_can_download_report(self):
        allowed, _ = check_access(
            "GET", f"/api/v1/report/{LOCKER_UUID}/download", Role.OPERATOR, [], False,
        )
        assert allowed

    def test_worker_cannot_delete_report(self):
        allowed, _ = check_access(
            "DELETE", f"/api/v1/report/{LOCKER_UUID}", Role.WORKER, [], False,
        )
        assert not allowed

    def test_operator_can_delete_report(self):
        allowed, _ = check_access(
            "DELETE", f"/api/v1/report/{LOCKER_UUID}", Role.OPERATOR, [], False,
        )
        assert allowed

    def test_worker_cannot_rename_report(self):
        allowed, _ = check_access(
            "PATCH", f"/api/v1/report/{LOCKER_UUID}", Role.WORKER, [], False,
        )
        assert not allowed

    def test_operator_can_rename_report(self):
        allowed, _ = check_access(
            "PATCH", f"/api/v1/report/{LOCKER_UUID}", Role.OPERATOR, [], False,
        )
        assert allowed


# ---------------------------------------------------------------------------
# Регрессия аудита стенда 2026-06-11
# ---------------------------------------------------------------------------

class TestStandAuditRegressions:
    def test_admin_can_resolve_security_event(self):
        """PATCH /events/security/{id} — раньше default-deny даже для admin."""
        allowed, reason = check_access(
            "PATCH", "/api/v1/events/security/2846", Role.ADMINISTRATOR, [], False,
        )
        assert allowed, reason

    def test_operator_can_resolve_security_event(self):
        allowed, _ = check_access(
            "PATCH", "/api/v1/events/security/2846", Role.OPERATOR, [], False,
        )
        assert allowed

    def test_worker_cannot_resolve_security_event(self):
        allowed, _ = check_access(
            "PATCH", "/api/v1/events/security/2846", Role.WORKER, [], False,
        )
        assert not allowed

    def test_operator_cannot_post_external_ingest(self):
        for path in (
            "/api/v1/events/security",
            "/api/v1/telemetry",
            "/api/v1/commands/history",
        ):
            allowed, _ = check_access("POST", path, Role.OPERATOR, [], False)
            assert not allowed, f"POST {path} must be administrator-only"

    def test_token_revoke_is_not_public(self):
        """Правило отзыва токенов должно перебивать публичное ^/api/v1/auth/."""
        rule = find_rule("DELETE", "/api/v1/auth/users/7/tokens")
        assert rule is not None
        assert rule.min_role == Role.ADMINISTRATOR

    def test_token_revoke_other_auth_paths_still_public(self):
        rule = find_rule("POST", "/api/v1/auth/login")
        assert rule is not None
        assert rule.min_role is None

    def test_worker_cannot_read_locker_sensors(self):
        """GET sensors было затенено широким GET ^/api/v1/locker/ (WORKER)."""
        allowed, _ = check_access(
            "GET", f"/api/v1/locker/{LOCKER_UUID}/sensors",
            Role.WORKER, [LOCKER_UUID], False,
        )
        assert not allowed

    def test_operator_can_delete_locker_sensor(self):
        """DELETE sensors было затенено DELETE ^/api/v1/locker/ (ADMINISTRATOR)."""
        allowed, _ = check_access(
            "DELETE", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc",
            Role.OPERATOR, [], False,
        )
        assert allowed

    def test_operator_cannot_create_threshold(self):
        """POST thresholds было затенено POST .../sensors (OPERATOR)."""
        allowed, _ = check_access(
            "POST", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds",
            Role.OPERATOR, [], False,
        )
        assert not allowed

    def test_worker_keeps_locker_detail_access(self):
        """Реордеринг не должен отнять у worker GET /locker/{id} с Допуском."""
        allowed, _ = check_access(
            "GET", f"/api/v1/locker/{LOCKER_UUID}",
            Role.WORKER, [LOCKER_UUID], False,
        )
        assert allowed

    def test_worker_keeps_commands_access(self):
        allowed, _ = check_access(
            "POST", f"/api/v1/locker/{LOCKER_UUID}/commands",
            Role.WORKER, [LOCKER_UUID], False,
        )
        assert allowed

    def test_operator_denied_delete_threshold_detail(self):
        """DELETE .../thresholds/{id} — оператор не должен иметь доступ."""
        allowed, _ = check_access(
            "DELETE", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds/5",
            Role.OPERATOR, [], False,
        )
        assert not allowed

    def test_administrator_allowed_delete_threshold_detail(self):
        """DELETE .../thresholds/{id} — администратор должен иметь доступ."""
        allowed, _ = check_access(
            "DELETE", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds/5",
            Role.ADMINISTRATOR, [], False,
        )
        assert allowed

    def test_operator_denied_put_threshold_detail(self):
        """PUT .../thresholds/{id} — оператор не должен иметь доступ."""
        allowed, _ = check_access(
            "PUT", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds/5",
            Role.OPERATOR, [], False,
        )
        assert not allowed

    def test_administrator_allowed_put_threshold_detail(self):
        """PUT .../thresholds/{id} — администратор должен иметь доступ."""
        allowed, _ = check_access(
            "PUT", f"/api/v1/locker/{LOCKER_UUID}/sensors/abc/thresholds/5",
            Role.ADMINISTRATOR, [], False,
        )
        assert allowed

    def test_auth_users_trailing_slash_is_not_public(self):
        """DELETE /auth/users/7/tokens/ (trailing slash) не должен быть публичным."""
        rule = find_rule("DELETE", "/api/v1/auth/users/7/tokens/")
        assert rule is not None
        assert rule.min_role == Role.ADMINISTRATOR

    def test_auth_users_get_is_not_public(self):
        """GET /auth/users/7/tokens не должен проваливаться в публичное правило."""
        rule = find_rule("GET", "/api/v1/auth/users/7/tokens")
        assert rule is not None
        assert rule.min_role == Role.ADMINISTRATOR

    def test_worker_without_grant_uppercase_uuid_denied(self):
        """Worker без Допуска + UUID в верхнем регистре → отказ (не bypass)."""
        allowed, reason = check_access(
            "POST", f"/api/v1/locker/{LOCKER_UUID_UPPER}/commands",
            Role.WORKER, [], False,
        )
        assert not allowed

    def test_worker_with_grant_uppercase_uuid_allowed(self):
        """Worker с Допуском (lowercase в списке) + uppercase UUID в пути → разрешено."""
        allowed, _ = check_access(
            "POST", f"/api/v1/locker/{LOCKER_UUID_UPPER}/commands",
            Role.WORKER, [LOCKER_UUID], False,  # grant хранится в lowercase
        )
        assert allowed
