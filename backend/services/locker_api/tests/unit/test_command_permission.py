"""Unit-тесты для матрицы прав command_type ↔ role.

Покрывают `shared.access_control.check_command_permission`.
Матрица закреплена в `backend/shared/access_control/commands.py`. Тесты
здесь живут в locker_api, потому что locker_api — единственный потребитель
shared-модуля.

Покрытие:
- Все 12 (роль × command_type) комбинаций согласно утверждённой матрице.
- Edge cases: пустая роль, неизвестная роль, неизвестный command_type.
"""
from __future__ import annotations

import unittest

from shared.access_control import (
    COMMAND_PERMISSION_MATRIX,
    check_command_permission,
)


class TestCheckCommandPermission(unittest.TestCase):
    # ------------------------------------------------------------------
    # WORKER: только open/close
    # ------------------------------------------------------------------
    def test_worker_open_allowed(self):
        self.assertTrue(check_command_permission("worker", "open"))

    def test_worker_close_allowed(self):
        self.assertTrue(check_command_permission("worker", "close"))

    def test_worker_reboot_denied(self):
        self.assertFalse(check_command_permission("worker", "reboot"))

    def test_worker_config_denied(self):
        self.assertFalse(check_command_permission("worker", "config"))

    # ------------------------------------------------------------------
    # OPERATOR: всё разрешено
    # ------------------------------------------------------------------
    def test_operator_open_allowed(self):
        self.assertTrue(check_command_permission("operator", "open"))

    def test_operator_close_allowed(self):
        self.assertTrue(check_command_permission("operator", "close"))

    def test_operator_reboot_allowed(self):
        self.assertTrue(check_command_permission("operator", "reboot"))

    def test_operator_config_denied(self):
        """CONFIG выведен из эксплуатации — отказ даже operator'у."""
        self.assertFalse(check_command_permission("operator", "config"))

    # ------------------------------------------------------------------
    # ADMINISTRATOR: всё разрешено (super-set operator'а)
    # ------------------------------------------------------------------
    def test_administrator_open_allowed(self):
        self.assertTrue(check_command_permission("administrator", "open"))

    def test_administrator_close_allowed(self):
        self.assertTrue(check_command_permission("administrator", "close"))

    def test_administrator_reboot_allowed(self):
        self.assertTrue(check_command_permission("administrator", "reboot"))

    def test_administrator_config_denied(self):
        """CONFIG выведен из эксплуатации — отказ даже administrator'у."""
        self.assertFalse(check_command_permission("administrator", "config"))

    def test_relay_commands_only_for_operator_and_administrator(self):
        for ctype in ("relay_on", "relay_off", "relay_restart"):
            self.assertFalse(check_command_permission("worker", ctype), ctype)
            self.assertTrue(check_command_permission("operator", ctype), ctype)
            self.assertTrue(check_command_permission("administrator", ctype), ctype)

    # ------------------------------------------------------------------
    # Edge cases — default deny
    # ------------------------------------------------------------------
    def test_empty_role_denied(self):
        """Пустая X-Remote-Role (gateway не выставил) → отказ."""
        self.assertFalse(check_command_permission("", "open"))

    def test_unknown_role_denied(self):
        """Неизвестная роль (не в UserRole) → отказ."""
        self.assertFalse(check_command_permission("superadmin", "open"))
        self.assertFalse(check_command_permission("WORKER", "open"))  # case-sensitive

    def test_unknown_command_denied_for_admin(self):
        """Неизвестный command_type → отказ даже для admin."""
        self.assertFalse(check_command_permission("administrator", "factory_reset"))
        self.assertFalse(check_command_permission("administrator", ""))

    def test_none_role_denied(self):
        """None как роль (если caller не привёл к строке) → отказ."""
        self.assertFalse(check_command_permission(None, "open"))  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Структура матрицы
    # ------------------------------------------------------------------
    def test_matrix_keys_match_user_roles(self):
        """Ключи матрицы — все три UserRole, без лишних."""
        self.assertEqual(
            set(COMMAND_PERMISSION_MATRIX.keys()),
            {"worker", "operator", "administrator"},
        )

    def test_operator_is_superset_of_worker(self):
        """Иерархия: operator может всё что worker."""
        worker_set = COMMAND_PERMISSION_MATRIX["worker"]
        operator_set = COMMAND_PERMISSION_MATRIX["operator"]
        self.assertTrue(worker_set.issubset(operator_set))

    def test_administrator_is_superset_of_operator(self):
        """Иерархия: admin может всё что operator (сейчас равны)."""
        operator_set = COMMAND_PERMISSION_MATRIX["operator"]
        admin_set = COMMAND_PERMISSION_MATRIX["administrator"]
        self.assertTrue(operator_set.issubset(admin_set))


if __name__ == "__main__":
    unittest.main()
