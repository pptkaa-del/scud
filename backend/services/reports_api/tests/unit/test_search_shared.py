"""Unit-тесты для общего модуля подстрочного поиска ``shared.search``.

Модуль чистый (без БД/IO) — тесты обычные `def test_*` с прямыми проверками.
Размещены в reports_api по тому же принципу, что и `acm/.../test_redis_client.py`
для `shared.redis_client`: shared-модуль покрывается в сервисе-потребителе.
"""
from __future__ import annotations

import pytest

from shared.search import build_like_pattern, escape_like, like_filter, normalize_query

# ---------------------------------------------------------------------------
# escape_like
# ---------------------------------------------------------------------------


def test_escape_like_empty():
    assert escape_like("") == ""


def test_escape_like_plain_unchanged():
    assert escape_like("abc") == "abc"


def test_escape_like_percent():
    assert escape_like("50%") == "50\\%"


def test_escape_like_underscore():
    assert escape_like("a_b") == "a\\_b"


def test_escape_like_backslash_first():
    # Один backslash на входе → два на выходе (LIKE-нотация).
    assert escape_like("a\\b") == "a\\\\b"


def test_escape_like_backslash_before_percent():
    # Backslash экранируется первым: `\%` → `\\\%`, а не `\\%`.
    assert escape_like("\\%") == "\\\\\\%"


def test_escape_like_mixed():
    assert escape_like("a%b_c\\d") == "a\\%b\\_c\\\\d"


# ---------------------------------------------------------------------------
# build_like_pattern
# ---------------------------------------------------------------------------


def test_build_like_pattern_wraps_with_percent():
    assert build_like_pattern("abc") == "%abc%"


def test_build_like_pattern_escapes_then_wraps():
    # Литеральный `%` экранируется, обрамляющие `%` — нет.
    assert build_like_pattern("50%") == "%50\\%%"


# ---------------------------------------------------------------------------
# like_filter
# ---------------------------------------------------------------------------


def test_like_filter_single_column():
    clause, params = like_filter("foo", "name")
    assert clause == "(name ILIKE %s ESCAPE '\\')"
    assert params == ["%foo%"]


def test_like_filter_multi_column_or():
    clause, params = like_filter("foo", "inventory_number", "location_address")
    assert clause == (
        "(inventory_number ILIKE %s ESCAPE '\\'"
        " OR location_address ILIKE %s ESCAPE '\\')"
    )
    assert params == ["%foo%", "%foo%"]


def test_like_filter_escapes_metachars_in_params():
    _, params = like_filter("a_b", "name")
    assert params == ["%a\\_b%"]


def test_like_filter_requires_column():
    with pytest.raises(ValueError):
        like_filter("foo")


# ---------------------------------------------------------------------------
# normalize_query
# ---------------------------------------------------------------------------


def test_normalize_query_none():
    assert normalize_query(None, max_length=100) is None


def test_normalize_query_empty_is_none():
    assert normalize_query("", max_length=100) is None


def test_normalize_query_whitespace_is_none():
    assert normalize_query("   ", max_length=100) is None


def test_normalize_query_trims():
    assert normalize_query("  abc  ", max_length=100) == "abc"


def test_normalize_query_passes_through():
    assert normalize_query("Ленина", max_length=100) == "Ленина"


def test_normalize_query_too_long_raises():
    with pytest.raises(ValueError, match="must not exceed 5"):
        normalize_query("x" * 6, max_length=5)


def test_normalize_query_at_limit_ok():
    assert normalize_query("x" * 5, max_length=5) == "x" * 5
