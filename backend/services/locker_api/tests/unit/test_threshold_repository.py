"""Unit-тесты для ``_normalize_for_compare`` и ``normalize_boolean_fixed_value``.

Для бинарных датчиков с порогом ``fixed_value="false"`` нормализация не
должна давать ложного нарушения, даже когда в БД лежит числовое 0.0.
"""
import unittest

from app.locker_data.db.repositories.threshold import (
    _normalize_for_compare,
    normalize_boolean_fixed_value,
)


class TestNormalizeForCompare(unittest.TestCase):
    def test_python_bool_true(self):
        self.assertEqual(_normalize_for_compare(True, "true"), "true")

    def test_python_bool_false(self):
        self.assertEqual(_normalize_for_compare(False, "false"), "false")

    def test_float_zero_with_false_fixed(self):
        # Defensive layer: legacy {"v": 0.0} в БД остаётся корректно
        # сравнимым с fixed_value="false".
        self.assertEqual(_normalize_for_compare(0.0, "false"), "false")

    def test_float_one_with_true_fixed(self):
        self.assertEqual(_normalize_for_compare(1.0, "true"), "true")

    def test_int_zero_with_false_fixed(self):
        self.assertEqual(_normalize_for_compare(0, "false"), "false")

    def test_numeric_with_numeric_fixed_unchanged(self):
        # Numeric path не должен ломаться defensive layer'ом.
        self.assertEqual(_normalize_for_compare(50.0, "50.0"), "50.0")

    def test_string_with_string_fixed_unchanged(self):
        self.assertEqual(_normalize_for_compare("open", "open"), "open")

    def test_string_true_with_false_fixed_treated_as_boolean(self):
        # Защитный слой: строка от устройства тоже трактуется по boolean
        # формату fixed_value.
        self.assertEqual(_normalize_for_compare("True", "false"), "true")
        self.assertEqual(_normalize_for_compare("FALSE", "false"), "false")


class TestNormalizeBooleanFixedValue(unittest.TestCase):
    """Канонизация ``fixed_value`` для boolean-датчика на write-boundary."""

    def test_canonical_lowercase_unchanged(self):
        self.assertEqual(normalize_boolean_fixed_value("true"), "true")
        self.assertEqual(normalize_boolean_fixed_value("false"), "false")

    def test_capitalized_normalized(self):
        self.assertEqual(normalize_boolean_fixed_value("True"), "true")
        self.assertEqual(normalize_boolean_fixed_value("False"), "false")
        self.assertEqual(normalize_boolean_fixed_value("TRUE"), "true")

    def test_numeric_string_normalized(self):
        self.assertEqual(normalize_boolean_fixed_value("1"), "true")
        self.assertEqual(normalize_boolean_fixed_value("0"), "false")

    def test_python_bool_normalized(self):
        self.assertEqual(normalize_boolean_fixed_value(True), "true")
        self.assertEqual(normalize_boolean_fixed_value(False), "false")

    def test_whitespace_stripped(self):
        self.assertEqual(normalize_boolean_fixed_value(" True "), "true")

    def test_invalid_raises_value_error(self):
        with self.assertRaises(ValueError):
            normalize_boolean_fixed_value("banana")
        with self.assertRaises(ValueError):
            normalize_boolean_fixed_value("2")


if __name__ == "__main__":
    unittest.main()
