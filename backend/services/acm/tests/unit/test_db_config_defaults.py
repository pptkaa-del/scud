"""
SEC-24 unit: дефолты подключения ACM к БД указывают на least-privilege роль.

Проверяем реальный построитель конфига main.build_db_config(): что без
выставленных env/secret ACM по умолчанию ходит под acm_ro и читает пароль из
секрета acm_db_password (dev-default), а НЕ под admin/postgres_password, и что
env-override (DB_USER) уважается.
"""
import os
import unittest
from unittest import mock

from app.main import build_db_config

# Должны совпадать с дефолтами в app/main.py / docker-compose / seed-dev-secrets.
EXPECTED_DB_USER = "acm_ro"
EXPECTED_PASSWORD_DEFAULT = "acm_ro_dev_password"


class TestAcmDbConfigDefaults(unittest.TestCase):
    def test_default_user_is_acm_ro(self):
        # Пустое окружение и отсутствие secret-файла → least-privilege дефолты.
        with mock.patch.dict(os.environ, {}, clear=True):
            config = build_db_config()
        self.assertEqual(config.user, EXPECTED_DB_USER)
        self.assertEqual(config.password, EXPECTED_PASSWORD_DEFAULT)
        self.assertEqual(config.database, "scud_users")
        self.assertEqual(config.application_name, "acm")

    def test_db_user_env_override_respected(self):
        with mock.patch.dict(os.environ, {"DB_USER": "custom_ro"}, clear=True):
            config = build_db_config()
        self.assertEqual(config.user, "custom_ro")

    def test_db_password_env_override_respected(self):
        with mock.patch.dict(os.environ, {"DB_PASSWORD": "from-env"}, clear=True):
            config = build_db_config()
        self.assertEqual(config.password, "from-env")


if __name__ == "__main__":
    unittest.main()
