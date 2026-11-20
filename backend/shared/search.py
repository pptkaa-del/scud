"""Унифицированный подстрочный поиск (ILIKE) для списочных ручек.

Все списочные эндпоинты, поддерживающие текстовый поиск, используют единый
query-параметр `q` и хелперы из этого модуля:

- handler: :func:`normalize_query` — trim, пусто/пробелы → ``None`` (фильтр не
  применяется), превышение длины → :class:`ValueError` (handler отдаёт ``400``);
- repository: :func:`like_filter` — строит SQL-фрагмент
  ``(<col> ILIKE %s ESCAPE '\\' OR ...)`` и список параметров с экранированными
  LIKE-метасимволами.

LIKE-метасимволы (``%``, ``_``) и сам экранирующий ``\\`` в пользовательском
вводе экранируются и ищутся как литералы: ``q=50%`` находит буквальный знак
процента, а не «всё».

БЕЗОПАСНОСТЬ (SQL-injection):
    Значение ``q`` ВСЕГДА передаётся через bind-параметр (``%s``) и безопасно.
    А вот ИМЕНА КОЛОНОК (``*columns`` в :func:`like_filter`) интерполируются в
    текст SQL напрямую — идентификаторы нельзя параметризовать. Поэтому в
    позицию колонки разрешено подставлять ТОЛЬКО константные/whitelisted имена,
    зашитые в коде репозитория (напр. ``"u.full_name"``, ``"inventory_number"``).
    НИКОГДА не передавайте сюда имя колонки, пришедшее из запроса/тела/заголовка
    пользователя — это прямой вектор SQL-инъекции. :func:`like_filter`
    дополнительно проверяет форму идентификатора как defence-in-depth, но это
    НЕ замена whitelist'у на стороне caller'а.
"""
from __future__ import annotations

import re

# Допустимая форма идентификатора колонки: опциональный префикс таблицы/алиаса
# (``alias.``) + имя; только буквы/цифры/подчёркивание. Намеренно НЕ допускает
# кавычки, пробелы, скобки — это защита от случайной интерполяции
# пользовательского ввода в позицию колонки (см. блок БЕЗОПАСНОСТЬ выше).
_SAFE_COLUMN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")

# Суффикс ILIKE-сравнения: объявляет `\` экранирующим символом, чтобы
# заэкранированные в `escape_like` метасимволы трактовались как литералы.
_ILIKE_ESCAPE = " ESCAPE '\\'"


def escape_like(value: str) -> str:
    """Экранировать LIKE-метасимволы (``%``, ``_``, ``\\``) в пользовательском вводе.

    Backslash экранируется ПЕРВЫМ, иначе он удвоится при экранировании
    ``%``/``_``. Возвращает строку БЕЗ обрамляющих ``%…%`` — оборачивает caller
    (см. :func:`build_like_pattern`).
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_like_pattern(value: str) -> str:
    """``%<escaped>%`` — готовый параметр для ``<col> ILIKE %s ESCAPE '\\'``."""
    return f"%{escape_like(value)}%"


def like_filter(q: str, *columns: str) -> tuple[str, list[str]]:
    """SQL-фрагмент и параметры подстрочного поиска по одной или нескольким колонкам.

    Возвращает кортеж ``(clause, params)``:

    - ``clause`` — ``(<col1> ILIKE %s ESCAPE '\\' OR <col2> ILIKE %s ESCAPE '\\')``
      (без префикса ``AND``; caller добавляет его при склейке WHERE);
    - ``params`` — список из ``build_like_pattern(q)``, повторённый по числу колонок.

    ``q`` ожидается уже нормализованным (trim, непустой) — см. :func:`normalize_query`.

    .. warning::
        ``columns`` интерполируются в текст SQL напрямую (идентификаторы нельзя
        параметризовать). Передавайте ТОЛЬКО константные/whitelisted имена,
        зашитые в код репозитория — НИКОГДА имя колонки из пользовательского
        ввода. См. блок «БЕЗОПАСНОСТЬ» в docstring модуля. Форма каждого
        идентификатора дополнительно валидируется ниже (defence-in-depth).
    """
    if not columns:
        raise ValueError("like_filter requires at least one column")
    for col in columns:
        if not _SAFE_COLUMN.match(col):
            raise ValueError(f"unsafe column identifier for like_filter: {col!r}")
    pattern = build_like_pattern(q)
    clause = "(" + " OR ".join(f"{col} ILIKE %s{_ILIKE_ESCAPE}" for col in columns) + ")"
    return clause, [pattern] * len(columns)


def normalize_query(raw: str | None, *, max_length: int) -> str | None:
    """Нормализовать значение параметра ``q`` из query-string.

    trim → пустая строка/только пробелы → ``None`` (фильтр не применяется).
    Превышение ``max_length`` → :class:`ValueError` (handler отдаёт ``400``).
    """
    if raw is None:
        return None
    trimmed = raw.strip()
    if not trimmed:
        return None
    if len(trimmed) > max_length:
        raise ValueError(f"q must not exceed {max_length} characters")
    return trimmed
