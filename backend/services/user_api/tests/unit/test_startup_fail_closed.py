"""Регрессия: production fail-closed загрузки секретов в user_api (SEC-11).

make_app() читает 5 internal HMAC-ключей через read_secret(required=is_production()).
В production (APP_ENV in {production,prod}) пустой или placeholder-ключ ОБЯЗАН
валить старт RuntimeError'ом, чтобы misconfigured deploy не поднялся на слабом
секрете. В dev/CI (APP_ENV не задан) механизм fail-open: make_app() поднимается,
а ненастроенные ключи просто не попадают в settings.

make_app(db, cache_invalidator=None) не делает DB/network при чтении секретов,
поэтому db можно подменить MagicMock'ом и звать make_app напрямую.
"""
import os
import unittest
from unittest import mock
from unittest.mock import MagicMock

from app.main import make_app

# (env-имя ключа, его dev-placeholder из seed-dev-secrets.sh). Каждый читается
# в make_app через read_secret(required=is_production()).
USER_API_INTERNAL_KEYS = (
    ("INTERNAL_AUTH_HMAC_KEY", "internal-auth-dev-key-do-not-use-in-prod"),
    ("INTERNAL_MAX_BINDING_HMAC_KEY", "internal-max-binding-dev-key-do-not-use-in-prod"),
    ("INTERNAL_USER_READ_HMAC_KEY", "internal-user-read-dev-key-do-not-use-in-prod"),
    ("INTERNAL_STEP_UP_HMAC_KEY", "internal-step-up-dev-key-do-not-use-in-prod"),
    ("INTERNAL_SESSION_REVOKE_HMAC_KEY", "internal-session-revoke-dev-key-do-not-use-in-prod"),
)


class TestUserApiStartupFailClosed(unittest.TestCase):
    def test_production_placeholder_key_raises(self) -> None:
        # Для каждого из 5 ключей: APP_ENV=production + dev-placeholder → RuntimeError.
        for env_var, placeholder in USER_API_INTERNAL_KEYS:
            with self.subTest(env_var=env_var), \
                    mock.patch.dict(
                        os.environ,
                        {"APP_ENV": "production", env_var: placeholder},
                        clear=True,
                    ), \
                    self.assertRaises(RuntimeError):
                make_app(MagicMock())

    def test_production_empty_key_raises(self) -> None:
        # Пустая строка (ключ не сконфигурирован) в production тоже валит старт.
        for env_var, _ in USER_API_INTERNAL_KEYS:
            with self.subTest(env_var=env_var), \
                    mock.patch.dict(
                        os.environ,
                        {"APP_ENV": "production", env_var: ""},
                        clear=True,
                    ), \
                    self.assertRaises(RuntimeError):
                make_app(MagicMock())

    def test_dev_no_keys_does_not_raise_and_keeps_fail_open(self) -> None:
        # APP_ENV не задан, ключей нет: make_app поднимается (fail-open), а
        # ненастроенные internal-ключи НЕ попадают в settings.
        with mock.patch.dict(os.environ, {}, clear=True):
            app = make_app(MagicMock())
        for settings_key in (
            "internal_auth_hmac_key",
            "internal_max_binding_hmac_key",
            "internal_user_read_hmac_key",
            "internal_step_up_hmac_key",
        ):
            self.assertNotIn(settings_key, app.settings)


if __name__ == "__main__":
    unittest.main()
