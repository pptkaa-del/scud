"""Unit-тесты ``LockerScope`` — области видимости шкафов из ``X-Remote-Lockers``.

``require_locker_grant`` отвечает на вопрос «пустить ли к ОДНОМУ шкафу», но
списочные эндпоинты задают другой вопрос: «какие строки вообще показывать».
``resolve_locker_scope`` — второй вид того же Допуска, поэтому ключевой тест
здесь — матрица эквивалентности: оба API обязаны решать одинаково при любых
входах, иначе scalar-гейт и row-фильтр разъедутся.

Инвариант fail-closed: пустой/отсутствующий/неизвестный контекст даёт ПУСТУЮ
область (ноль строк), а НЕ unrestricted.
"""
from shared.access_control import (
    LockerScope,
    require_locker_grant,
    resolve_locker_scope,
)

LOCKER_A = "550e8400-e29b-41d4-a716-446655440000"
LOCKER_B = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
A_DASHLESS = "550e8400e29b41d4a716446655440000"
A_BRACED = "{550E8400-E29B-41D4-A716-446655440000}"


# ── unrestricted: роль и full_access ──────────────────────────────────────

def test_operator_role_is_unrestricted_regardless_of_header() -> None:
    """operator не ограничен Допуском на конкретный шкаф — как в require_locker_grant."""
    scope = resolve_locker_scope("", "operator")
    assert scope.unrestricted
    assert scope.sql_filter_ids() is None


def test_administrator_role_is_unrestricted_regardless_of_header() -> None:
    scope = resolve_locker_scope("", "administrator")
    assert scope.unrestricted


def test_role_matching_is_case_and_whitespace_insensitive() -> None:
    """Зеркалит нормализацию роли в require_locker_grant."""
    assert resolve_locker_scope("", "  Administrator ").unrestricted
    assert resolve_locker_scope("", "OPERATOR").unrestricted


def test_full_access_marker_is_unrestricted() -> None:
    """worker с full_access Допуском получает '*' от ACM."""
    scope = resolve_locker_scope("*", "worker")
    assert scope.unrestricted
    assert scope.sql_filter_ids() is None


# ── worker: ограниченная область ──────────────────────────────────────────

def test_worker_with_comma_list_gets_canonical_scope() -> None:
    scope = resolve_locker_scope(f"{LOCKER_A},{LOCKER_B}", "worker")
    assert not scope.unrestricted
    assert scope.locker_ids == frozenset({LOCKER_A, LOCKER_B})


def test_worker_with_empty_header_gets_empty_scope() -> None:
    """Нет Допусков → ноль строк, но это НЕ ошибка и НЕ unrestricted."""
    scope = resolve_locker_scope("", "worker")
    assert not scope.unrestricted
    assert scope.is_empty
    assert scope.sql_filter_ids() == []


def test_worker_with_none_header_gets_empty_scope() -> None:
    scope = resolve_locker_scope(None, "worker")
    assert not scope.unrestricted
    assert scope.is_empty


def test_unknown_role_gets_empty_scope_not_unrestricted() -> None:
    """Fail-closed: неизвестная/отсутствующая роль НЕ открывает весь парк."""
    for role in ("", "  ", "root", "admin", "superuser"):
        scope = resolve_locker_scope("", role)
        assert not scope.unrestricted, f"role={role!r} fail-open!"
        assert scope.sql_filter_ids() == []


# ── SEC-02: канонизация обеих сторон ──────────────────────────────────────

def test_dashless_braced_uppercase_uuids_canonicalize_to_same_member() -> None:
    """Любая форма UUID в гранте нормализуется к каноническому виду."""
    for form in (A_DASHLESS, A_BRACED, LOCKER_A.upper(), f"  {LOCKER_A}  "):
        scope = resolve_locker_scope(form, "worker")
        assert scope.locker_ids == frozenset({LOCKER_A}), f"form={form!r}"
        assert scope.allows(LOCKER_A)


def test_unparseable_grants_are_dropped() -> None:
    scope = resolve_locker_scope(f"not-a-uuid,,{LOCKER_A},42", "worker")
    assert scope.locker_ids == frozenset({LOCKER_A})


def test_unparseable_target_denied_even_when_unrestricted() -> None:
    """То, что БД не резолвит как UUID, не пропускаем даже администратору."""
    assert not resolve_locker_scope("*", "administrator").allows("not-a-uuid")


# ── sql_filter_ids: None ≠ [] ─────────────────────────────────────────────

def test_sql_filter_ids_returns_none_when_unrestricted() -> None:
    """None — «фильтра нет», отдельно от [] — «фильтр в ноль строк»."""
    assert resolve_locker_scope("*", "worker").sql_filter_ids() is None


def test_sql_filter_ids_returns_empty_list_for_zero_grants() -> None:
    assert resolve_locker_scope("", "worker").sql_filter_ids() == []


def test_sql_filter_ids_is_sorted_and_deterministic() -> None:
    scope = resolve_locker_scope(f"{LOCKER_B},{LOCKER_A}", "worker")
    assert scope.sql_filter_ids() == sorted([LOCKER_A, LOCKER_B])


def test_scope_is_immutable() -> None:
    """frozen dataclass — область нельзя расширить после разбора заголовка."""
    scope = resolve_locker_scope(LOCKER_A, "worker")
    assert isinstance(scope, LockerScope)
    try:
        scope.unrestricted = True  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("LockerScope must be immutable")


# ── анти-дрейф: два API одного Допуска ────────────────────────────────────

def test_require_locker_grant_equivalence_matrix() -> None:
    """require_locker_grant(h, r, t) == resolve_locker_scope(h, r).allows(t).

    Гейт против расхождения scalar-проверки (команды) и row-фильтра (списки):
    сегодня это одна функция, и тест обязан ловить любую попытку их развести.
    """
    roles = ["worker", "operator", "administrator", "", "ADMIN", "  worker  "]
    headers = [None, "", "*", LOCKER_A, f"{LOCKER_A},{LOCKER_B}", A_DASHLESS,
               f"garbage,{LOCKER_A}"]
    targets = [LOCKER_A, LOCKER_B, "not-a-uuid", A_DASHLESS]

    for role in roles:
        for header in headers:
            for target in targets:
                expected = require_locker_grant(header, role, target)
                actual = resolve_locker_scope(header, role).allows(target)
                assert expected == actual, (
                    f"drift: role={role!r} header={header!r} target={target!r} "
                    f"require={expected} scope.allows={actual}"
                )
