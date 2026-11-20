"""Матрица прав на управление пользователями (роль caller'а ↔ target).

ACM URL-rule (``acm/app/rules.py``) пропускает любого OPERATOR на
``PATCH /api/v1/user/{id}`` и 2FA-toggle. Этот модуль — следующий уровень:
fine-grained проверка, которую ACM не может выполнить, не зная роль caller'а,
роль target'а и запрашиваемую новую роль.

Используется из ``user_api`` (PATCH handler, enable/disable-2fa). Чистая
Python-логика, без сети и без Tornado — поэтому юнит-тестируема в изоляции.

Иерархия ролей (по возрастанию привилегий): worker < operator < administrator.

Правила (default-deny при неизвестной/пустой роли caller'а):

1. Caller НЕ может назначить ``role`` строго выше собственной роли.
2. Caller НЕ может изменять привилегированные поля (role / password / 2FA)
   target'а, чья ТЕКУЩАЯ роль >= роли caller'а. Исключение: administrator
   может изменять кого угодно (включая других администраторов).
3. Caller НЕ может повысить собственную роль (self-promotion).
"""
from __future__ import annotations

from shared.enums import UserRole

# Числовой ранг роли. Чем выше — тем больше привилегий. Источник истины
# для всех сравнений «строго выше / >=» в этом модуле.
ROLE_RANK: dict[str, int] = {
    UserRole.WORKER.value: 1,
    UserRole.OPERATOR.value: 2,
    UserRole.ADMINISTRATOR.value: 3,
}


def _rank(role: str | None) -> int | None:
    """Ранг роли или None для пустой/неизвестной роли (default-deny)."""
    if not role:
        return None
    return ROLE_RANK.get(role)


def can_assign_role(caller_role: str | None, new_role: str | None) -> bool:
    """True если caller вправе назначить ``new_role`` (правило 1).

    Caller не может назначить роль строго выше собственной. Назначение
    равной либо более низкой роли разрешено. Пустая/неизвестная роль
    caller'а или назначаемая роль → отказ.
    """
    caller_rank = _rank(caller_role)
    target_rank = _rank(new_role)
    if caller_rank is None or target_rank is None:
        return False
    return target_rank <= caller_rank


def can_modify_target(caller_role: str | None, target_role: str | None) -> bool:
    """True если caller вправе менять привилегированные поля target'а (правило 2).

    Привилегированные поля — ``role`` / ``password`` / enable-2fa / disable-2fa.
    Administrator может менять кого угодно. Иначе caller может менять только
    target'ов со СТРОГО более низкой ролью (operator → worker, но не operator
    → operator и не operator → administrator). Пустая/неизвестная роль
    caller'а → отказ.
    """
    caller_rank = _rank(caller_role)
    target_rank = _rank(target_role)
    if caller_rank is None:
        return False
    if caller_role == UserRole.ADMINISTRATOR.value:
        return True
    # Неизвестная роль target'а трактуется как потенциально привилегированная —
    # default-deny для не-администраторов.
    if target_rank is None:
        return False
    return target_rank < caller_rank


def is_self_promotion(
    caller_role: str | None,
    new_role: str | None,
    *,
    caller_is_target: bool,
) -> bool:
    """True если caller пытается повысить собственную роль (правило 3).

    Применимо только когда caller изменяет сам себя (``caller_is_target``).
    Назначение себе равной/более низкой роли self-promotion'ом не считается.
    """
    if not caller_is_target or new_role is None:
        return False
    caller_rank = _rank(caller_role)
    target_rank = _rank(new_role)
    if caller_rank is None or target_rank is None:
        # Неизвестная роль caller'а/новая роль — пусть отбракует can_assign_role;
        # здесь self-promotion'ом такое не считаем.
        return False
    return target_rank > caller_rank
