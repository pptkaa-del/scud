"""Unit: TokenBucket.refund возвращает токен, не превышая ёмкость."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from shared.utils.rate_limit import TokenBucket


class TestTokenBucketRefund(unittest.TestCase):
    def test_refund_returns_consumed_token(self) -> None:
        with patch("shared.utils.rate_limit.time.monotonic", return_value=100.0):
            bucket = TokenBucket(rate=0.001, capacity=1.0)
            self.assertTrue(bucket.try_consume())
            self.assertFalse(bucket.try_consume())
            bucket.refund()
            self.assertTrue(bucket.try_consume())

    def test_refund_is_capped_at_capacity(self) -> None:
        with patch("shared.utils.rate_limit.time.monotonic", return_value=100.0):
            bucket = TokenBucket(rate=0.001, capacity=2.0)
            bucket.refund(5.0)
            self.assertTrue(bucket.try_consume(2.0))
            self.assertFalse(bucket.try_consume())


if __name__ == "__main__":
    unittest.main()
