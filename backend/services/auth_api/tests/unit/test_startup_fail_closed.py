"""Регрессия: production fail-closed загрузки секретов в auth_api (SEC-11).

auth_api читает секреты внутри async main() ПОСЛЕ wait_for_db(), поэтому
вызывать main() в unit-тесте нельзя (нужна живая БД). Вместо этого проверяем
инвариант напрямую через shared.utils.read_secret с теми же name/env_var/default,
что использует auth_api/app/main.py:
  - jwt_secret      / JWT_SECRET                         / default 'dev-secret-change-in-production'
  - 6 internal_*    / INTERNAL_*_HMAC_KEY                / default ''
Каждый читается с required=is_production().

В production (APP_ENV in {production,prod}) пустой/placeholder/default-секрет
ОБЯЗАН валить старт RuntimeError'ом. В dev/CI (APP_ENV не задан) — fail-open:
возвращается значение/'' без исключения.
"""
import os
import unittest
from unittest import mock

from shared.utils import is_production, read_secret

# jwt_secret: (secret-имя, env-имя, dev-default из main.py).
_JWT = ("jwt_secret", "JWT_SECRET", "dev-secret-change-in-production")

# 6 internal HMAC-ключей: (secret-имя, env-имя, dev-placeholder из seed-скрипта).
# default в main.py — пустая строка; placeholder приходит из секрет-файла/env.
AUTH_API_INTERNAL_KEYS = (
    ("internal_auth_hmac_key", "INTERNAL_AUTH_HMAC_KEY",
     "internal-auth-dev-key-do-not-use-in-prod"),
    ("internal_otp_hmac_key", "INTERNAL_OTP_HMAC_KEY",
     "internal-otp-dev-key-do-not-use-in-prod"),
    ("internal_step_up_hmac_key", "INTERNAL_STEP_UP_HMAC_KEY",
     "internal-step-up-dev-key-do-not-use-in-prod"),
    ("internal_user_read_hmac_key", "INTERNAL_USER_READ_HMAC_KEY",
     "internal-user-read-dev-key-do-not-use-in-prod"),
    ("internal_session_revoke_hmac_key", "INTERNAL_SESSION_REVOKE_HMAC_KEY",
     "internal-session-revoke-dev-key-do-not-use-in-prod"),
    ("internal_history_hmac_key", "INTERNAL_HISTORY_HMAC_KEY",
     "internal-history-dev-key-do-not-use-in-prod"),
)


class TestAuthApiStartupFailClosed(unittest.TestCase):
    def test_production_jwt_default_raises(self) -> None:
        # JWT не задан → итог = default 'dev-secret-change-in-production' (placeholder).
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=True), \
                self.assertRaises(RuntimeError):
            read_secret(
                _JWT[0], env_var=_JWT[1], default=_JWT[2],
                required=is_production(),
            )

    def test_production_jwt_empty_raises(self) -> None:
        with mock.patch.dict(
            os.environ, {"APP_ENV": "production", "JWT_SECRET": ""}, clear=True,
        ), self.assertRaises(RuntimeError):
            read_secret(
                _JWT[0], env_var=_JWT[1], default=_JWT[2],
                required=is_production(),
            )

    def test_production_internal_keys_placeholder_raise(self) -> None:
        for name, env_var, placeholder in AUTH_API_INTERNAL_KEYS:
            with self.subTest(env_var=env_var), \
                    mock.patch.dict(
                        os.environ,
                        {"APP_ENV": "production", env_var: placeholder},
                        clear=True,
                    ), \
                    self.assertRaises(RuntimeError):
                read_secret(
                    name, env_var=env_var, default="",
                    required=is_production(),
                )

    def test_production_internal_keys_empty_raise(self) -> None:
        for name, env_var, _ in AUTH_API_INTERNAL_KEYS:
            with self.subTest(env_var=env_var), \
                    mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=True), \
                    self.assertRaises(RuntimeError):
                # default='' и env не задан → итог пустой → RuntimeError.
                read_secret(
                    name, env_var=env_var, default="",
                    required=is_production(),
                )

    def test_dev_jwt_default_returns_without_raising(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                read_secret(
                    _JWT[0], env_var=_JWT[1], default=_JWT[2],
                    required=is_production(),
                ),
                _JWT[2],
            )

    def test_dev_internal_keys_return_empty_without_raising(self) -> None:
        for name, env_var, _ in AUTH_API_INTERNAL_KEYS:
            with self.subTest(env_var=env_var), \
                    mock.patch.dict(os.environ, {}, clear=True):
                self.assertEqual(
                    read_secret(
                        name, env_var=env_var, default="",
                        required=is_production(),
                    ),
                    "",
                )


if __name__ == "__main__":
    unittest.main()
