"""
Матрица разрешений ACM.

Реализует правила из ACM.md:
- Иерархия ролей: administrator > operator > worker
- Все не разрешённые явно действия — запрещены
- Для шкафов worker проверяется наличие активного Допуска отдельно в CheckHandler

Структура правила: (method, path_pattern, min_role)
  method       — HTTP-метод или "*" (любой)
  path_pattern — regex для пути запроса
  min_role     — минимальная роль; None = публичный доступ без аутентификации
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
import re

from shared.access_control import canonical_locker_uuid, canonicalize_grant_set


class Role(IntEnum):
    """Иерархия ролей. Значение = уровень доступа (чем выше, тем больше прав)."""
    WORKER = 1
    OPERATOR = 2
    ADMINISTRATOR = 3

    @classmethod
    def from_str(cls, value: str) -> Role:
        mapping = {
            "worker": cls.WORKER,
            "operator": cls.OPERATOR,
            "administrator": cls.ADMINISTRATOR,
        }
        try:
            return mapping[value.lower()]
        except KeyError:
            raise ValueError(f"Unknown role: {value!r}") from None


@dataclass(frozen=True, slots=True)
class Rule:
    """Одно правило матрицы разрешений."""
    method: str  # HTTP-метод ("GET", "POST", ...) или "*"
    path_pattern: str  # regex
    min_role: Role | None  # None — публичный (без проверки токена)

    def matches(self, method: str, path: str) -> bool:
        """Проверяет, применимо ли правило к данному методу и пути."""
        method_match = self.method == "*" or self.method.upper() == method.upper()
        return method_match and bool(re.match(self.path_pattern, path))

    def allows(self, role: Role) -> bool:
        """Проверяет, достаточно ли роли для выполнения действия."""
        if self.min_role is None:
            return True
        return role >= self.min_role


# ---------------------------------------------------------------------------
# Матрица разрешений
# Правила проверяются по порядку; срабатывает первое совпадение.
# ---------------------------------------------------------------------------

RULES: list[Rule] = [
    # ------------------------------------------------------------------
    # Публичные эндпоинты (без токена)
    # ВАЖНО: вся ветка /auth/users/ — административная; правило стоит
    # ДО широкого ^/api/v1/auth/: варианты пути (trailing slash, другие методы)
    # не должны проваливаться в публичное правило и давать fail-open.
    # ------------------------------------------------------------------
    Rule("*", r"^/api/v1/auth/users/", Role.ADMINISTRATOR),
    Rule("*", r"^/api/v1/auth/", None),
    Rule("GET", r"^/health$", None),

    # ------------------------------------------------------------------
    # Аварийные заявки
    # worker — только при наличии Допуска на указанный шкаф.
    # ACM пропускает на уровне роли; fine-grained проверка Допуска по
    # locker_id из тела (POST) и scoping чтения на свои заявки (GET) —
    # на стороне user_api (handlers/emergency_request.py, SEC-08/09).
    # ------------------------------------------------------------------
    Rule("POST", r"^/api/v1/emergency-requests$", Role.WORKER),
    Rule("GET", r"^/api/v1/emergency-requests", Role.WORKER),
    Rule("GET", r"^/api/v1/emergency-request/\d+/logs$", Role.OPERATOR),
    Rule("GET", r"^/api/v1/emergency-request/\d+$", Role.WORKER),
    Rule("DELETE", r"^/api/v1/emergency-request/\d+$", Role.OPERATOR),

    # ------------------------------------------------------------------
    # Пользователи
    # ------------------------------------------------------------------
    Rule("GET", r"^/api/v1/users", Role.OPERATOR),
    Rule("POST", r"^/api/v1/users$", Role.OPERATOR),
    Rule("GET", r"^/api/v1/user/\d+$", Role.OPERATOR),
    Rule("PATCH", r"^/api/v1/user/\d+$", Role.OPERATOR),
    Rule("DELETE", r"^/api/v1/user/\d+$", Role.OPERATOR),
    Rule("POST", r"^/api/v1/user/\d+/reactivate$", Role.OPERATOR),
    Rule("POST", r"^/api/v1/user/\d+/enable-2fa$", Role.OPERATOR),
    Rule("POST", r"^/api/v1/user/\d+/disable-2fa$", Role.OPERATOR),

    # ------------------------------------------------------------------
    # Допуски
    # ------------------------------------------------------------------
    Rule("POST", r"^/api/v1/user/\d+/accesses$", Role.OPERATOR),
    Rule("GET", r"^/api/v1/user/\d+/accesses$", Role.OPERATOR),
    Rule("GET", r"^/api/v1/access/\d+$", Role.OPERATOR),
    Rule("PATCH", r"^/api/v1/access/\d+$", Role.OPERATOR),
    Rule("DELETE", r"^/api/v1/access/\d+$", Role.OPERATOR),
    Rule("POST", r"^/api/v1/access/\d+/reactivate$", Role.OPERATOR),
    Rule("POST", r"^/api/v1/accesses/batch$", Role.OPERATOR),

    # ------------------------------------------------------------------
    # Подрядчики
    # ------------------------------------------------------------------
    Rule("GET", r"^/api/v1/contractors", Role.OPERATOR),
    Rule("POST", r"^/api/v1/contractors$", Role.OPERATOR),
    Rule("GET", r"^/api/v1/contractor/", Role.OPERATOR),
    Rule("PUT", r"^/api/v1/contractor/", Role.OPERATOR),
    Rule("DELETE", r"^/api/v1/contractor/", Role.OPERATOR),

    # ------------------------------------------------------------------
    # Шкафы
    # GET /api/v1/lockers (коллекция) — в пути нет UUID, поэтому ACM гейтит
    # только по роли. Построчная фильтрация по Допуску ДЕЛЕГИРОВАНА
    # locker_api через заголовок X-Remote-Lockers (ACM читает scud_users и
    # структурно не может фильтровать строки scud_lockers).
    # GET /api/v1/locker/{id} (экземпляр) — locker-scoped, Допуск проверяется
    # здесь же, в ACM (см. is_locker_scoped/check_access ниже).
    # POST /commands — worker+Допуск пропускается на ACM-уровне; тонкая
    # проверка command_type против роли реализована в
    # `backend/shared/access_control/commands.py` (используется из
    # `locker_api/handlers/command.py`). Матрица:
    #   worker         → OPEN, CLOSE
    #   operator/admin → OPEN, CLOSE, REBOOT, CONFIG
    # ------------------------------------------------------------------
    Rule("GET", r"^/api/v1/lockers", Role.WORKER),
    Rule("POST", r"^/api/v1/lockers$", Role.OPERATOR),
    Rule("POST", r"^/api/v1/lockers/batch$", Role.OPERATOR),
    # Специфичные под-ресурсы шкафа стоят ДО широких правил ^/api/v1/locker/:
    # first-match иначе отдаёт их широкому правилу с неверной ролью
    # (GET/DELETE sensors и POST thresholds были затенены до 2026-06-11).
    Rule("POST", r"^/api/v1/locker/[^/]+/commands$", Role.WORKER),
    Rule("POST", r"^/api/v1/locker/[^/]+/commands/confirm$", Role.WORKER),
    Rule("GET", r"^/api/v1/locker/[^/]+/relays", Role.WORKER),
    Rule("PATCH", r"^/api/v1/locker/[^/]+/relays", Role.ADMINISTRATOR),
    Rule("GET", r"^/api/v1/locker/[^/]+/sensors/[^/]+/thresholds", Role.OPERATOR),
    Rule("POST", r"^/api/v1/locker/[^/]+/sensors/[^/]+/thresholds", Role.ADMINISTRATOR),
    # пороги определяют генерацию security-событий — изменение/удаление только администратору (как и создание).
    Rule("PUT", r"^/api/v1/locker/[^/]+/sensors/[^/]+/thresholds", Role.ADMINISTRATOR),
    Rule("DELETE", r"^/api/v1/locker/[^/]+/sensors/[^/]+/thresholds", Role.ADMINISTRATOR),
    Rule("GET", r"^/api/v1/locker/[^/]+/sensors", Role.OPERATOR),
    Rule("POST", r"^/api/v1/locker/[^/]+/sensors", Role.OPERATOR),
    Rule("PATCH", r"^/api/v1/locker/[^/]+/sensors/", Role.OPERATOR),
    # ON DELETE CASCADE удаляет и пороги датчика — осознанно операторский уровень (восстановлен записанный intent матрицы).
    Rule("DELETE", r"^/api/v1/locker/[^/]+/sensors/", Role.OPERATOR),
    # Широкие правила экземпляра шкафа
    Rule("GET", r"^/api/v1/locker/", Role.WORKER),
    Rule("PUT", r"^/api/v1/locker/", Role.OPERATOR),
    Rule("PATCH", r"^/api/v1/locker/", Role.OPERATOR),
    Rule("DELETE", r"^/api/v1/locker/", Role.ADMINISTRATOR),
    Rule("GET", r"^/api/v1/sensor-types", Role.OPERATOR),
    Rule("POST", r"^/api/v1/sensor-types", Role.ADMINISTRATOR),
    Rule("PUT", r"^/api/v1/sensor-types/\d+$", Role.ADMINISTRATOR),
    Rule("DELETE", r"^/api/v1/sensor-types/\d+$", Role.ADMINISTRATOR),

    # ------------------------------------------------------------------
    # Уведомления
    # GET — worker может получать уведомления (persistent, нацеленные на него)
    # POST/DELETE — только operator+
    # ------------------------------------------------------------------
    Rule("GET", r"^/api/v1/notifications", Role.WORKER),
    Rule("POST", r"^/api/v1/notifications$", Role.OPERATOR),
    Rule("GET", r"^/api/v1/notification/[0-9a-f-]{36}$", Role.WORKER),
    Rule("DELETE", r"^/api/v1/notification/[0-9a-f-]{36}$", Role.OPERATOR),

    # ------------------------------------------------------------------
    # История
    # ------------------------------------------------------------------
    Rule("GET", r"^/api/v1/telemetry", Role.OPERATOR),
    Rule("GET", r"^/api/v1/events/", Role.OPERATOR),
    Rule("GET", r"^/api/v1/commands/history", Role.OPERATOR),
    Rule("GET", r"^/api/v1/media", Role.OPERATOR),
    Rule("GET", r"^/api/v1/analytics/", Role.OPERATOR),
    # Write-пути history: закрытие события — операторская задача;
    # внешний ingest (дубли /internal/* для интеграций) — только админ.
    Rule("PATCH", r"^/api/v1/events/security/\d+$", Role.OPERATOR),
    Rule("POST", r"^/api/v1/events/security$", Role.ADMINISTRATOR),
    Rule("POST", r"^/api/v1/telemetry$", Role.ADMINISTRATOR),
    Rule("POST", r"^/api/v1/commands/history$", Role.ADMINISTRATOR),

    # ------------------------------------------------------------------
    # Отчёты
    # Генерация и управление отчётами — только operator+.
    # Специфичные правила (коллекция /reports) перед инстансами (/report/).
    # ------------------------------------------------------------------
    Rule("POST", r"^/api/v1/reports$", Role.OPERATOR),
    Rule("GET", r"^/api/v1/reports", Role.OPERATOR),
    Rule("GET", r"^/api/v1/report/", Role.OPERATOR),
    Rule("PATCH", r"^/api/v1/report/", Role.OPERATOR),
    Rule("DELETE", r"^/api/v1/report/", Role.OPERATOR),
]

# Эндпоинты, для которых worker должен иметь Допуск на конкретный шкаф.
# Regex захватывает СЫРОЙ locker-сегмент как первую группу — БЕЗ ограничения
# на форму UUID. Канонизация и parse-or-deny выполняются в is_locker_scoped.
#
# SEC-02: узкий паттерн `[0-9a-fA-F-]{36}` совпадал только с 36-символьным
# дефисным видом. 32-символьный dashless (`550e8400e29b...`) и braced
# (`{...}`) виды НЕ совпадали → is_locker_scoped → None → check_access
# ПРОПУСКАЛ проверку Допуска (а не запрещал) и проваливался в allow. При этом
# Postgres uuid принимает любую из этих форм и резолвит в реальный шкаф.
# Инвариант: всё, что locker_api зарезолвит как UUID, ACM обязан считать
# locker-scoped и проверять Допуск.
LOCKER_SCOPED_PATTERNS: list[str] = [
    r"^/api/v1/locker/([^/]+)(/|$)",
]

# Сентинел: путь locker-scoped, но сегмент не парсится как UUID. Отличим от
# None (не locker-scoped) — заставляет check_access ЗАПРЕТИТЬ, а не пропустить.
LOCKER_ID_UNPARSEABLE = "\x00unparseable"


def find_rule(method: str, path: str) -> Rule | None:
    """Возвращает первое совпавшее правило или None (запрещено)."""
    for rule in RULES:
        if rule.matches(method, path):
            return rule
    return None


def is_locker_scoped(path: str) -> str | None:
    """
    Классифицирует путь по обращению к конкретному шкафу.

    Возвращает:
        * канонический UUID (lowercase-дефисный) — путь locker-scoped и
          сегмент парсится как UUID (любая форма: dashed/dashless/braced/
          uppercase нормализуется к одному виду — как и в БД);
        * ``LOCKER_ID_UNPARSEABLE`` — путь locker-scoped, но сегмент НЕ UUID
          (заставляет check_access запретить, а не пропустить);
        * ``None`` — путь не относится к конкретному шкафу.

    Примеры:
        /api/v1/locker/550e8400-e29b-41d4-a716-446655440000          → UUID
        /api/v1/locker/550e8400e29b41d4a716446655440000/commands     → UUID (канонизирован)
        /api/v1/locker/{550E8400-...}                                → UUID (канонизирован)
        /api/v1/locker/not-a-uuid/commands                           → LOCKER_ID_UNPARSEABLE
        /api/v1/lockers                                              → None
    """
    for pattern in LOCKER_SCOPED_PATTERNS:
        m = re.match(pattern, path)
        if m:
            canonical = canonical_locker_uuid(m.group(1))
            return canonical if canonical is not None else LOCKER_ID_UNPARSEABLE
    return None


def check_access(
        method: str,
        path: str,
        role: Role,
        locker_ids: list[str],
        full_access: bool,
) -> tuple[bool, str]:
    """
    Основная функция проверки доступа.

    Args:
        method:      HTTP-метод запроса
        path:        Путь запроса (без query string)
        role:        Роль аутентифицированного пользователя
        locker_ids:  Список UUID шкафов с активным Допуском (для worker)
        full_access: True если у пользователя full_access Допуск

    Returns:
        (allowed, reason) — bool и строка с причиной отказа (пустая при allow)
    """
    rule = find_rule(method, path)

    if rule is None:
        return False, "No matching rule (deny by default)"

    # Публичный эндпоинт — всегда разрешён
    if rule.min_role is None:
        return True, ""

    # Проверка роли
    if not rule.allows(role):
        return False, f"Role {role.name.lower()!r} is below required {rule.min_role.name.lower()!r}"

    # Для worker проверяем Допуск на конкретный шкаф
    if role == Role.WORKER:
        locker_id = is_locker_scoped(path)
        if locker_id == LOCKER_ID_UNPARSEABLE:
            # Сегмент locker-scoped пути не парсится как UUID — запрещаем
            # (не пропускаем). SEC-02: иначе worker обошёл бы grant-чек.
            return False, "Unparseable locker id segment"
        if locker_id is not None and not full_access:
            # Канонизируем grant-список для сравнения: значения приходят из
            # БД/контекста и должны сверяться с канонизированным locker_id.
            # ТА ЖЕ функция, что канонизирует locker_id из пути (SEC-02).
            granted = canonicalize_grant_set(locker_ids)
            if locker_id not in granted:
                return False, f"No active access grant for locker {locker_id}"

    return True, ""
