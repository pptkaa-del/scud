"""Регрессия: production fail-closed загрузки секретов в locker_api (SEC-11).

_read_history_hmac_key() читает общий с history_api HMAC-ключ через
read_secret(required=is_production()) и не делает DB/network — его можно звать
напрямую. В production (APP_ENV in {production,prod}) пустой/placeholder-ключ
ОБЯЗАН валить старт RuntimeError'ом. В dev/CI (APP_ENV не задан) механизм
fail-open: возвращается None без исключения.
"""
import os
import unittest
from unittest import mock

from app.main import _read_history_hmac_key

_HISTORY_KEY_ENV = "INTERNAL_HISTORY_HMAC_KEY"
_HISTORY_PLACEHOLDER = "internal-history-dev-key-do-not-use-in-prod"


class TestLockerApiStartupFailClosed(unittest.TestCase):
    def test_production_placeholder_raises(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"APP_ENV": "production", _HISTORY_KEY_ENV: _HISTORY_PLACEHOLDER},
            clear=True,
        ), self.assertRaises(RuntimeError):
            _read_history_hmac_key()

    def test_production_empty_raises(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"APP_ENV": "production", _HISTORY_KEY_ENV: ""},
            clear=True,
        ), self.assertRaises(RuntimeError):
            _read_history_hmac_key()

    def test_dev_unset_returns_none(self) -> None:
        # APP_ENV не задан, ключа нет → fail-open: None без исключения.
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(_read_history_hmac_key())

    def test_production_real_key_returns_bytes(self) -> None:
        # В production реальный (не placeholder) ключ читается и кодируется.
        real_key = "a" * 64
        with mock.patch.dict(
            os.environ,
            {"APP_ENV": "production", _HISTORY_KEY_ENV: real_key},
            clear=True,
        ):
            self.assertEqual(_read_history_hmac_key(), real_key.encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
