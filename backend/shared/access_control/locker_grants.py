"""Допуск worker'а на шкафы: проверка одного шкафа и область видимости списка.

ACM уже проверяет наличие Допуска на URL-уровне (см. ``acm/app/rules.py``
``check_access``), но locker_api не должен слепо доверять тому, что запрос
прошёл через gateway. Этот модуль — второй независимый рубеж: handler
сверяет канонизированный ``locker_id`` со списком Допусков из заголовка
``X-Remote-Lockers``.

URL-level проверки ACM хватает для ОДНОГО шкафа (UUID есть в пути), но не для
коллекции ``GET /api/v1/lockers``: в её пути UUID нет, ``check_access`` решает
только по роли и физически не может отфильтровать строки (ACM читает
``scud_users``, а шкафы живут в ``scud_lockers``). Поэтому тот же Допуск нужен
в двух видах:

  * ``require_locker_grant`` — предикат «пустить ли к этому шкафу» (команды);
  * ``resolve_locker_scope`` → ``LockerScope`` — область «какие строки видно»
    (списки), которую репозиторий превращает в SQL-фильтр.

Оба вида ОБЯЗАНЫ решать одинаково: ``require_locker_grant`` реализован через
``LockerScope.allows``, а матрица эквивалентности в
``shared/tests/unit/test_locker_scope.py`` пресекает расхождение.

Ключевой инвариант (SEC-02): и целевой ``locker_id``, и значения из
``X-Remote-Lockers`` канонизируются через ``uuid.UUID`` ДО сравнения.
Иначе worker мог бы обойти проверку, передав не-канонический вид UUID
(32-символьный без дефисов, в фигурных скобках, в верхнем регистре) —
Postgres ``uuid`` принял бы его и резолвил в реальный шкаф, а наивное
строковое сравнение с grant-списком — нет.

Заголовок ``X-Remote-Lockers`` (выставляется ACM):
  * ``"*"``                       — full_access / operator / administrator;
  * ``"uuid1,uuid2"``             — список UUID шкафов с активным Допуском;
  * ``""`` (пусто/отсутствует)    — Допусков нет → deny для locker-scoped.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from uuid import UUID

from shared.enums import UserRole

# Маркер full_access в X-Remote-Lockers (operator/administrator/full_access worker).
FULL_ACCESS_MARKER = "*"

# Роли, не ограниченные Допуском на конкретный шкаф (управляются ролевыми
# правилами ACM). Единый источник для предиката и для области видимости.
_UNRESTRICTED_ROLES = frozenset({
    UserRole.OPERATOR.value,
    UserRole.ADMINISTRATOR.value,
})


def canonical_locker_uuid(segment: str) -> str | None:
    """Канонический lowercase-дефисный вид UUID шкафа или None, если не парсится.

    Единственный источник истины канонизации locker-UUID (SEC-02): и целевой
    идентификатор из пути, и значения из grant-списка ДОЛЖНЫ проходить через
    эту же функцию перед сравнением. Любая форма (dashed/dashless/braced/
    uppercase, с окружающими пробелами) нормализуется к одному виду — как и в БД.
    """
    try:
        return str(UUID(segment.strip()))
    except (ValueError, AttributeError):
        return None


def canonicalize_grant_set(grants: Iterable[str]) -> set[str]:
    """Канонизирует набор locker-UUID, отбрасывая непарсимые значения."""
    return {
        canon
        for grant in grants
        if (canon := canonical_locker_uuid(grant)) is not None
    }


@dataclass(frozen=True)
class LockerScope:
    """Множество шкафов, видимых вызывающему.

    ``unrestricted=True`` — ограничения нет (operator/administrator/full_access),
    ``locker_ids`` при этом не используется. Иначе видны ровно ``locker_ids`` —
    возможно, пустое множество (Допусков нет).

    Пустая область и отсутствие ограничения — РАЗНЫЕ вещи; их схлопывание и
    есть классический fail-open, поэтому они разведены и в типе, и в
    ``sql_filter_ids``.
    """

    unrestricted: bool
    locker_ids: frozenset[str]

    @property
    def is_empty(self) -> bool:
        """True если не видно ни одного шкафа (нет Допусков и нет full_access)."""
        return not self.unrestricted and not self.locker_ids

    def allows(self, locker_id: str) -> bool:
        """True если конкретный шкаф попадает в область видимости."""
        target = canonical_locker_uuid(locker_id)
        # Непарсимый id → deny ПЕРВЫМ делом, даже при unrestricted: то, что
        # резолвер БД примет как UUID, мы обязаны канонизировать и сверить.
        if target is None:
            return False
        return self.unrestricted or target in self.locker_ids

    def sql_filter_ids(self) -> list[str] | None:
        """Значение для SQL-фильтра ``id = ANY(...)``.

        ``None`` — фильтра НЕТ (unrestricted); ``[]`` — фильтр в НОЛЬ строк.
        Возвращать ``None`` вместо ``[]`` при пустых Допусках означало бы
        показать worker'у весь парк, поэтому эти случаи не схлопываются.
        Порядок детерминирован — стабильные SQL-параметры в тестах и логах.
        """
        return None if self.unrestricted else sorted(self.locker_ids)


# Область без ограничений (operator/administrator/full_access).
UNRESTRICTED = LockerScope(unrestricted=True, locker_ids=frozenset())


def resolve_locker_scope(
    remote_lockers_header: str | None,
    role: str,
) -> LockerScope:
    """Разбирает роль + ``X-Remote-Lockers`` в область видимости шкафов.

    Логика (роль решает раньше заголовка — как в ``require_locker_grant``):
      * operator / administrator → ``UNRESTRICTED`` (управляются ролевыми
        правилами ACM, Допуск на конкретный шкаф к ним не применяется);
      * ``X-Remote-Lockers == "*"`` → ``UNRESTRICTED`` (full_access);
      * иначе — ровно шкафы из списка Допусков (обе стороны канонизируются).

    Fail-closed: неизвестная/пустая роль с пустым заголовком даёт ПУСТУЮ
    область (ноль строк), а не ``UNRESTRICTED``.

    Args:
        remote_lockers_header: значение X-Remote-Lockers (может быть None/"").
        role: значение X-Remote-Role (регистр и пробелы не важны).
    """
    normalized_role = (role or "").strip().lower()
    if normalized_role in _UNRESTRICTED_ROLES:
        return UNRESTRICTED

    header = (remote_lockers_header or "").strip()
    if header == FULL_ACCESS_MARKER:
        return UNRESTRICTED

    return LockerScope(
        unrestricted=False,
        locker_ids=frozenset(canonicalize_grant_set(header.split(","))),
    )


def require_locker_grant(
    remote_lockers_header: str | None,
    role: str,
    locker_id: str,
) -> bool:
    """Возвращает True если роли+Допуску разрешён доступ к ``locker_id``.

    Тонкая обёртка над ``LockerScope.allows``: предикат для одного шкафа и
    фильтр для списка обязаны решать одинаково, поэтому правило ровно одно —
    ``resolve_locker_scope``.

    Args:
        remote_lockers_header: значение X-Remote-Lockers (может быть None/"").
        role: значение X-Remote-Role (регистр не важен).
        locker_id: идентификатор шкафа из пути.

    Returns:
        True если доступ разрешён, иначе False (handler отдаёт 403).
    """
    return resolve_locker_scope(remote_lockers_header, role).allows(locker_id)
