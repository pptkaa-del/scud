"""Unit-тесты для shared.utils.compute_request_signature / verify_request_signature.

Покрывает generic HMAC-подпись inter-service запросов после SEC-20: канон
привязан к method + path + sha256(body). Подпись, снятая для одного
(method, path), НЕ должна приниматься для другого — иначе захваченную в
30-секундном окне подпись можно переиграть против другого эндпойнта.

Кейсы:
1. round-trip: compute → verify → True для совпадающих method+path+body;
2. подмена path → False;
3. подмена method → False;
4. подмена body → False;
5. timestamp вне окна → False;
6. чужой ключ → False;
7. method регистронезависим (canon нормализует к upper);
8. malformed/пустые поля → False без исключения.
"""
from __future__ import annotations

import time
import unittest

from shared.utils import (
    body_sha256,
    compute_request_signature,
    signed_path,
    verify_request_signature,
)

_KEY = b"unit-test-key-do-not-use-in-prod"
_OTHER_KEY = b"different-key-also-test-only-yyy"
_BODY_HEX = body_sha256(b'{"phone":"+71112223344"}')
_EMPTY_HEX = body_sha256(b"")


class TestComputeRequestSignature(unittest.TestCase):
    """compute_request_signature: детерминирована и зависит от всех полей."""

    def test_returns_64_hex_chars(self) -> None:
        sig = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        self.assertEqual(len(sig), 64)
        int(sig, 16)

    def test_deterministic(self) -> None:
        a = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        b = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        self.assertEqual(a, b)

    def test_different_path_changes_signature(self) -> None:
        a = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        b = compute_request_signature("1700000000", "POST", "/b", _BODY_HEX, _KEY)
        self.assertNotEqual(a, b)

    def test_different_method_changes_signature(self) -> None:
        a = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        b = compute_request_signature("1700000000", "GET", "/a", _BODY_HEX, _KEY)
        self.assertNotEqual(a, b)

    def test_method_case_insensitive(self) -> None:
        a = compute_request_signature("1700000000", "post", "/a", _BODY_HEX, _KEY)
        b = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        self.assertEqual(a, b)

    def test_different_body_changes_signature(self) -> None:
        a = compute_request_signature("1700000000", "POST", "/a", _EMPTY_HEX, _KEY)
        b = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        self.assertNotEqual(a, b)

    def test_different_key_changes_signature(self) -> None:
        a = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _KEY)
        b = compute_request_signature("1700000000", "POST", "/a", _BODY_HEX, _OTHER_KEY)
        self.assertNotEqual(a, b)


class TestVerifyRequestSignature(unittest.TestCase):
    """verify_request_signature: round-trip + привязка к method/path + защиты."""

    def _now(self) -> str:
        return str(int(time.time()))

    def test_roundtrip_matching_method_path_returns_true(self) -> None:
        ts = self._now()
        sig = compute_request_signature(ts, "POST", "/internal/auth/check", _BODY_HEX, _KEY)
        self.assertTrue(
            verify_request_signature(
                sig, ts, "POST", "/internal/auth/check", _BODY_HEX, _KEY,
            )
        )

    def test_path_mismatch_returns_false(self) -> None:
        """Подпись для /a отвергается при проверке против /b (anti-replay SEC-20)."""
        ts = self._now()
        sig = compute_request_signature(ts, "GET", "/a", _EMPTY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, ts, "GET", "/b", _EMPTY_HEX, _KEY)
        )

    def test_method_mismatch_returns_false(self) -> None:
        """Подпись для GET отвергается при проверке против POST."""
        ts = self._now()
        sig = compute_request_signature(ts, "GET", "/a", _EMPTY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, ts, "POST", "/a", _EMPTY_HEX, _KEY)
        )

    def test_body_mismatch_returns_false(self) -> None:
        ts = self._now()
        sig = compute_request_signature(ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, ts, "POST", "/a", _EMPTY_HEX, _KEY)
        )

    def test_method_case_insensitive_roundtrip(self) -> None:
        ts = self._now()
        sig = compute_request_signature(ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertTrue(
            verify_request_signature(sig, ts, "post", "/a", _BODY_HEX, _KEY)
        )

    def test_wrong_key_returns_false(self) -> None:
        ts = self._now()
        sig = compute_request_signature(ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, ts, "POST", "/a", _BODY_HEX, _OTHER_KEY)
        )

    def test_expired_timestamp_returns_false(self) -> None:
        old_ts = str(int(time.time()) - 60)
        sig = compute_request_signature(old_ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, old_ts, "POST", "/a", _BODY_HEX, _KEY)
        )

    def test_future_timestamp_too_far_returns_false(self) -> None:
        future_ts = str(int(time.time()) + 60)
        sig = compute_request_signature(future_ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, future_ts, "POST", "/a", _BODY_HEX, _KEY)
        )

    def test_within_window_returns_true(self) -> None:
        ts = str(int(time.time()) - 25)
        sig = compute_request_signature(ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertTrue(
            verify_request_signature(sig, ts, "POST", "/a", _BODY_HEX, _KEY)
        )

    def test_malformed_timestamp_returns_false(self) -> None:
        sig = compute_request_signature("nan", "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, "nan", "POST", "/a", _BODY_HEX, _KEY)
        )

    def test_empty_signature_returns_false(self) -> None:
        ts = self._now()
        self.assertFalse(
            verify_request_signature("", ts, "POST", "/a", _BODY_HEX, _KEY)
        )

    def test_empty_method_returns_false(self) -> None:
        ts = self._now()
        sig = compute_request_signature(ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, ts, "", "/a", _BODY_HEX, _KEY)
        )

    def test_empty_path_returns_false(self) -> None:
        ts = self._now()
        sig = compute_request_signature(ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(sig, ts, "POST", "", _BODY_HEX, _KEY)
        )

    def test_empty_key_returns_false(self) -> None:
        ts = self._now()
        self.assertFalse(
            verify_request_signature("a" * 64, ts, "POST", "/a", _BODY_HEX, b"")
        )

    def test_custom_max_age_short_window(self) -> None:
        ts = str(int(time.time()) - 5)
        sig = compute_request_signature(ts, "POST", "/a", _BODY_HEX, _KEY)
        self.assertFalse(
            verify_request_signature(
                sig, ts, "POST", "/a", _BODY_HEX, _KEY, max_age_seconds=1,
            )
        )

    def test_short_signature_returns_false_without_raise(self) -> None:
        ts = self._now()
        self.assertFalse(
            verify_request_signature("deadbeef", ts, "POST", "/a", _BODY_HEX, _KEY)
        )


class TestSignedPath(unittest.TestCase):
    """signed_path: канонизирует query в подписываемый path (SEC-10)."""

    _PATH = "/internal/user/by-phone"

    def test_none_query_returns_path_unchanged(self) -> None:
        self.assertEqual(signed_path(self._PATH, None), self._PATH)

    def test_empty_query_returns_path_unchanged(self) -> None:
        self.assertEqual(signed_path(self._PATH, ""), self._PATH)

    def test_param_order_independent(self) -> None:
        """a=1&b=2 и b=2&a=1 дают одинаковый signed_path (детерминизм)."""
        self.assertEqual(
            signed_path(self._PATH, "a=1&b=2"),
            signed_path(self._PATH, "b=2&a=1"),
        )

    def test_value_change_changes_signed_path(self) -> None:
        self.assertNotEqual(
            signed_path(self._PATH, "phone=%2B71112223344"),
            signed_path(self._PATH, "phone=%2B79999999999"),
        )

    def test_url_encoded_value_roundtrips_symmetrically(self) -> None:
        """+7... закодированный как %2B7... даёт ту же каноническую строку.

        Подписант, который снимает подпись с qs из urlencode({'phone': '+7..'}),
        и проверяющий, который видит ту же wire-query, после parse_qsl+urlencode
        приходят к идентичной строке — round-trip симметричен.
        """
        from urllib.parse import urlencode
        phone = "+71112223344"
        wire_qs = urlencode({"phone": phone})  # phone=%2B71112223344
        # Verifier видит ровно wire_qs в request.query.
        signer_side = signed_path(self._PATH, wire_qs)
        verifier_side = signed_path(self._PATH, wire_qs)
        self.assertEqual(signer_side, verifier_side)
        # И значение действительно присутствует канонически (round-trip + проверка).
        self.assertIn("phone=%2B71112223344", signer_side)

    def test_includes_query_in_returned_path(self) -> None:
        result = signed_path(self._PATH, "ids=1%2C2%2C3")
        self.assertTrue(result.startswith(f"{self._PATH}?"))
        self.assertIn("ids=1%2C2%2C3", result)


class TestSignedPathRoundTrip(unittest.TestCase):
    """Полный round-trip signer→verifier через signed_path + сигнатуру (SEC-10)."""

    _PATH = "/internal/user/by-phone"

    def test_by_phone_roundtrip_succeeds_and_tamper_fails(self) -> None:
        from urllib.parse import urlencode
        ts = str(int(time.time()))
        phone = "+71112223344"
        # Signer: строит qs через urlencode, подписывает signed_path(path, qs).
        qs = urlencode({"phone": phone})
        sig = compute_request_signature(
            ts, "GET", signed_path(self._PATH, qs), _EMPTY_HEX, _KEY,
        )
        # Verifier: видит ту же wire-query, сворачивает тем же signed_path.
        self.assertTrue(
            verify_request_signature(
                sig, ts, "GET", signed_path(self._PATH, qs), _EMPTY_HEX, _KEY,
            )
        )
        # Атакующий подменил phone= в query после подписи → verify FALSE.
        tampered_qs = urlencode({"phone": "+79999999999"})
        self.assertFalse(
            verify_request_signature(
                sig, ts, "GET", signed_path(self._PATH, tampered_qs), _EMPTY_HEX, _KEY,
            )
        )
        # Старый стиль (подпись без query) тоже не проходит против query-bound.
        self.assertFalse(
            verify_request_signature(
                compute_request_signature(ts, "GET", self._PATH, _EMPTY_HEX, _KEY),
                ts, "GET", signed_path(self._PATH, qs), _EMPTY_HEX, _KEY,
            )
        )


if __name__ == "__main__":
    unittest.main()
