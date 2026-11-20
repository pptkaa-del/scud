"""Regression-тесты для shared.utils.hash_token.

Покрывает:
- детерминированность (same input → same output)
- формат (hex-строка длины 64, только [0-9a-f])
- timing-safe сравнение (regression-guard: hmac.compare_digest присутствует
  в verify.py — иначе legacy `==` сравнение восстанавливает timing-leak)
"""
from __future__ import annotations

import inspect
import re
import unittest

from app.handlers import verify as verify_module
from shared.utils import hash_token

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")


class TestHashToken(unittest.TestCase):

    def test_hash_token_deterministic(self):
        """hash_token('x') == hash_token('x') для любого входа."""
        for value in ("x", "abcdef", "very long token value " * 10, ""):
            self.assertEqual(hash_token(value), hash_token(value))

    def test_hash_token_returns_hex64(self):
        """hash_token возвращает 64-символьную lowercase hex-строку (SHA-256)."""
        result = hash_token("token123")
        self.assertEqual(len(result), 64)
        self.assertIsNotNone(_HEX64_RE.match(result))

    def test_hash_token_distinct_inputs_distinct_outputs(self):
        """Разные входы → разные хэши (sanity-check на отсутствие хардкода)."""
        self.assertNotEqual(hash_token("a"), hash_token("b"))
        self.assertNotEqual(hash_token("token"), hash_token("token "))


class TestVerifyUsesConstantTimeCompare(unittest.TestCase):
    """Regression: verify.py обязан сравнивать OTP-хэши через hmac.compare_digest."""

    def test_compare_uses_hmac_compare_digest(self):
        """В исходниках verify.py присутствует hmac.compare_digest (а не `==`)."""
        src = inspect.getsource(verify_module)
        self.assertIn(
            "hmac.compare_digest",
            src,
            "verify.py must use hmac.compare_digest for timing-safe OTP comparison",
        )


if __name__ == "__main__":
    unittest.main()
