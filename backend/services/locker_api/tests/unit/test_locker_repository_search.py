"""Unit-тесты LockerRepository: поиск шкафов по q (inventory_number OR location_address).

Покрывает:
- `escape_like` (shared.search): экранирование LIKE-специальных символов (`%`, `_`, `\\`).
- `get_all(q=...)`: добавление SQL-фрагмента и параметров.
- `count(q=...)`: тот же фрагмент симметрично с get_all (чтобы total и items
  считались по одному фильтру).
"""

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.locker_data.db.repositories.locker import LockerRepository
from shared.search import escape_like


class TestEscapeLikePattern(unittest.TestCase):
    """`escape_like` экранирует символы, которые LIKE трактует как метасимволы.

    Порядок важен: сначала backslash (потому что им же экранируются остальные),
    затем `%` и `_`. Возвращает строку БЕЗ обрамляющих `%…%` — это делает caller.
    """

    def test_empty_string_unchanged(self):
        self.assertEqual(escape_like(""), "")

    def test_plain_text_unchanged(self):
        self.assertEqual(escape_like("abc"), "abc")

    def test_percent_escaped(self):
        self.assertEqual(escape_like("50%"), "50\\%")

    def test_underscore_escaped(self):
        self.assertEqual(escape_like("a_b"), "a\\_b")

    def test_backslash_escaped(self):
        # Один backslash в input → два backslash в output (LIKE-нотация).
        self.assertEqual(escape_like("a\\b"), "a\\\\b")

    def test_backslash_escaped_first(self):
        """Сначала backslash, иначе двойное экранирование (`\\` → `\\\\` → `\\\\\\\\`).

        Корректно: `\\%` (input) → `\\\\%` (после `\\`-escape) → `\\\\\\%` (после `%`-escape).
        """
        self.assertEqual(escape_like("\\%"), "\\\\\\%")

    def test_mixed(self):
        self.assertEqual(escape_like("a%b_c\\d"), "a\\%b\\_c\\\\d")


class TestGetAllWithQ(unittest.TestCase):
    """`get_all(q=...)` должен прицепить (inventory_number ILIKE %s OR location_address ILIKE %s)
    к WHERE и положить экранированный `%abc%` в params ДВАЖДЫ."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_all = AsyncMock(return_value=[])
        self.repo = LockerRepository(self.db_mock)

    async def _async_q_present(self):
        await self.repo.get_all(q="abc")

        sql = self.db_mock.fetch_all.call_args[0][0]
        params = self.db_mock.fetch_all.call_args[0][1]

        self.assertIn("inventory_number ILIKE %s ESCAPE '\\'", sql)
        self.assertIn("location_address ILIKE %s ESCAPE '\\'", sql)
        # Один и тот же `%abc%` фигурирует в params дважды — для inventory и address.
        self.assertEqual(params.count("%abc%"), 2)

    def test_get_all_q_appends_filter_and_double_param(self):
        asyncio.run(self._async_q_present())

    async def _async_q_escaped(self):
        # `%` и `_` в пользовательском вводе должны быть экранированы перед wrap.
        await self.repo.get_all(q="50%_x")
        params = self.db_mock.fetch_all.call_args[0][1]
        self.assertEqual(params.count("%50\\%\\_x%"), 2)

    def test_get_all_q_escapes_like_special_chars(self):
        asyncio.run(self._async_q_escaped())

    async def _async_q_absent(self):
        # q=None → SQL не должен содержать ILIKE-фрагмент (обратная совместимость).
        await self.repo.get_all()
        sql = self.db_mock.fetch_all.call_args[0][0]
        self.assertNotIn("ILIKE", sql)

    def test_get_all_q_none_does_not_add_filter(self):
        asyncio.run(self._async_q_absent())


class TestCountWithQ(unittest.TestCase):
    """`count(q=...)` симметрично с get_all — иначе total разъедется с items."""

    def setUp(self):
        self.db_mock = MagicMock()
        self.db_mock.fetch_one = AsyncMock(return_value={"count": 0})
        self.repo = LockerRepository(self.db_mock)

    async def _async(self):
        await self.repo.count(q="abc")

        sql = self.db_mock.fetch_one.call_args[0][0]
        params = self.db_mock.fetch_one.call_args[0][1]

        self.assertIn("inventory_number ILIKE %s ESCAPE '\\'", sql)
        self.assertIn("location_address ILIKE %s ESCAPE '\\'", sql)
        self.assertEqual(params.count("%abc%"), 2)

    def test_count_q_appends_filter_and_double_param(self):
        asyncio.run(self._async())


if __name__ == "__main__":
    unittest.main()
