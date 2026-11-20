"""Тонкая матрица прав на уровне ниже ACM URL-rules.

ACM-микросервис делает URL-level check (см. ``acm/app/rules.py``). Этот
shared-пакет — слой fine-grained проверок, которые ACM не может выполнить:
без чтения тела запроса (``command_type`` в JSON body) или без доступа к
строкам чужой БД (шкафы живут в ``scud_lockers``, ACM читает ``scud_users``).

Модули:
  * ``commands`` — матрица command_type ↔ роль;
  * ``relays`` — политика каналов блока реле (``authorize_relay_command``);
  * ``locker_grants`` — Допуск на шкафы: предикат для одного шкафа
    (``require_locker_grant``) и область видимости для списков
    (``resolve_locker_scope`` → ``LockerScope``);
  * ``user_management`` — ранги ролей и защита от self-promotion.
"""
from .commands import (
    COMMAND_PERMISSION_MATRIX,
    OTP_REQUIRED_COMMANDS,
    RELAY_COMMAND_TYPES,
    check_command_permission,
)
from .locker_grants import (
    FULL_ACCESS_MARKER,
    UNRESTRICTED,
    LockerScope,
    canonical_locker_uuid,
    canonicalize_grant_set,
    require_locker_grant,
    resolve_locker_scope,
)
from .relays import (
    RELAY_CHANNEL_MAX,
    RELAY_CHANNEL_MIN,
    RELAY_PROBLEM_STATUS,
    LockerRelayPolicy,
    RelayDecision,
    RelayProblem,
    allowed_relay_actions,
    authorize_relay_command,
    is_relay_command,
)
from .user_management import (
    ROLE_RANK,
    can_assign_role,
    can_modify_target,
    is_self_promotion,
)

__all__ = [
    "COMMAND_PERMISSION_MATRIX",
    "FULL_ACCESS_MARKER",
    "OTP_REQUIRED_COMMANDS",
    "RELAY_CHANNEL_MAX",
    "RELAY_CHANNEL_MIN",
    "RELAY_COMMAND_TYPES",
    "RELAY_PROBLEM_STATUS",
    "ROLE_RANK",
    "UNRESTRICTED",
    "LockerRelayPolicy",
    "LockerScope",
    "RelayDecision",
    "RelayProblem",
    "allowed_relay_actions",
    "authorize_relay_command",
    "can_assign_role",
    "can_modify_target",
    "canonical_locker_uuid",
    "canonicalize_grant_set",
    "check_command_permission",
    "is_relay_command",
    "is_self_promotion",
    "require_locker_grant",
    "resolve_locker_scope",
]
