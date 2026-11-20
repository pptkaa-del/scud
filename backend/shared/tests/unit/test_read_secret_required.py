"""Тесты fail-closed режима read_secret и helper is_production (SEC-11).

read_secret(required=True) обязан кидать RuntimeError, когда итоговое
значение отсутствует / пустое / является известным placeholder'ом —
чтобы misconfigured production не стартовал на слабом секрете.
required=False (дефолт) сохраняет прежнее тихое поведение.
"""
import os
import unittest
from unittest import mock

from shared.utils import _KNOWN_PLACEHOLDER_SECRETS, is_production, read_secret

# Dev-default значения, которые backend/scripts/seed-dev-secrets.sh кладёт в
# /run/secrets для jwt_secret + 7 internal_*_hmac_key. Каждое ОБЯЗАНО входить в
# shared.utils._KNOWN_PLACEHOLDER_SECRETS, иначе production-деплой с засеянными
# dev-секретами поднимется на слабом ключе (read_secret(required=True) его не
# отбракует). Этот кортеж — drift-guard: при правке seed-скрипта правь и его.
SEED_DEV_DEFAULTS = (
    # jwt_secret dev-default (seed-dev-secrets.sh) + исторический literal,
    # который ещё фигурирует как default= в auth_api/app/main.py.
    "change-me-in-production-must-be-at-least-32-bytes-long",
    "dev-secret-change-in-production",
    # internal_*_hmac_key dev-defaults (seed-dev-secrets.sh ~lines 64-70).
    "internal-auth-dev-key-do-not-use-in-prod",
    "internal-otp-dev-key-do-not-use-in-prod",
    "internal-max-binding-dev-key-do-not-use-in-prod",
    "internal-user-read-dev-key-do-not-use-in-prod",
    "internal-session-revoke-dev-key-do-not-use-in-prod",
    "internal-step-up-dev-key-do-not-use-in-prod",
    "internal-history-dev-key-do-not-use-in-prod",
)


class TestIsProduction(unittest.TestCase):
    def test_default_is_not_production(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(is_production())

    def test_development_is_not_production(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "development"}, clear=True):
            self.assertFalse(is_production())

    def test_production_value(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "production"}, clear=True):
            self.assertTrue(is_production())

    def test_prod_alias(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "prod"}, clear=True):
            self.assertTrue(is_production())

    def test_unknown_value_is_not_production(self) -> None:
        with mock.patch.dict(os.environ, {"APP_ENV": "staging"}, clear=True):
            self.assertFalse(is_production())


class TestReadSecretRequired(unittest.TestCase):
    def test_required_missing_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaises(RuntimeError):
            read_secret("nonexistent_secret", env_var="NO_SUCH_VAR", required=True)

    def test_required_empty_env_raises(self) -> None:
        with mock.patch.dict(os.environ, {"SOME_VAR": ""}, clear=True), \
                self.assertRaises(RuntimeError):
            read_secret("nonexistent_secret", env_var="SOME_VAR", required=True)

    def test_required_placeholder_default_raises(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True), \
                self.assertRaises(RuntimeError):
            read_secret(
                "nonexistent_secret",
                env_var="NO_SUCH_VAR",
                default="change-me-in-production-must-be-at-least-32-bytes-long",
                required=True,
            )

    def test_required_hmac_dev_placeholder_raises(self) -> None:
        # Dev-значения, которые seed-dev-secrets.sh кладёт в /run/secrets для
        # HMAC-ключей, обязаны трактоваться как «секрет не задан» — иначе
        # production-деплой с засеянными dev-секретами поднимется на слабом ключе.
        dev_placeholders = (
            "internal-auth-dev-key-do-not-use-in-prod",
            "internal-otp-dev-key-do-not-use-in-prod",
            "internal-max-binding-dev-key-do-not-use-in-prod",
            "internal-user-read-dev-key-do-not-use-in-prod",
            "internal-session-revoke-dev-key-do-not-use-in-prod",
            "internal-step-up-dev-key-do-not-use-in-prod",
            "internal-history-dev-key-do-not-use-in-prod",
            "scud_backend_dev_password",
            "redis-dev-only-do-not-use-in-prod",
            "password",
            "changeme",
        )
        for placeholder in dev_placeholders:
            with self.subTest(placeholder=placeholder), \
                    mock.patch.dict(os.environ, {"SOME_VAR": placeholder}, clear=True), \
                    self.assertRaises(RuntimeError):
                read_secret("nonexistent_secret", env_var="SOME_VAR", required=True)

    def test_required_real_value_returns(self) -> None:
        with mock.patch.dict(os.environ, {"SOME_VAR": "real-strong-secret"}, clear=True):
            self.assertEqual(
                read_secret("nonexistent_secret", env_var="SOME_VAR", required=True),
                "real-strong-secret",
            )

    def test_required_real_64_hex_secret_returns(self) -> None:
        # 64-символьный hex (как из `openssl rand -hex 32`) не должен ложно
        # совпасть ни с одним placeholder'ом — exact-match без substring-логики.
        real_secret = "a" * 64
        with mock.patch.dict(os.environ, {"SOME_VAR": real_secret}, clear=True):
            self.assertEqual(
                read_secret("nonexistent_secret", env_var="SOME_VAR", required=True),
                real_secret,
            )

    def test_not_required_missing_returns_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                read_secret("nonexistent_secret", env_var="NO_SUCH_VAR", default="fallback"),
                "fallback",
            )

    def test_not_required_empty_env_returns_empty(self) -> None:
        with mock.patch.dict(os.environ, {"SOME_VAR": ""}, clear=True):
            self.assertEqual(
                read_secret("nonexistent_secret", env_var="SOME_VAR", default="fallback"),
                "",
            )


class TestSeedScriptPlaceholderDriftGuard(unittest.TestCase):
    """Drift-guard: каждый dev-default из seed-dev-secrets.sh обязан числиться
    в _KNOWN_PLACEHOLDER_SECRETS, иначе fail-closed его пропустит."""

    def test_every_seed_dev_default_is_a_known_placeholder(self) -> None:
        for value in SEED_DEV_DEFAULTS:
            with self.subTest(value=value):
                self.assertIn(
                    value,
                    _KNOWN_PLACEHOLDER_SECRETS,
                    msg=(
                        f"Seed dev-default {value!r} is not in "
                        "_KNOWN_PLACEHOLDER_SECRETS — production deploy seeded "
                        "with dev secrets would boot on a weak key. Add it to "
                        "shared.utils._KNOWN_PLACEHOLDER_SECRETS."
                    ),
                )

    def test_every_seed_dev_default_trips_required_read_secret(self) -> None:
        # Сквозная проверка: значение-placeholder реально валит required-чтение.
        for value in SEED_DEV_DEFAULTS:
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"SOME_VAR": value}, clear=True), \
                    self.assertRaises(RuntimeError):
                read_secret("nonexistent_secret", env_var="SOME_VAR", required=True)


if __name__ == "__main__":
    unittest.main()
