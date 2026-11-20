"""Unit-тесты shared.database.config.PostgresConfig.

Фиксируют валидацию ctor (port/timeout/непустые строки), frozen-семантику и то,
что фасад connection.PostgresConfig — ровно тот же объект, что и
config.PostgresConfig (реэкспорт, не копия).

Живёт в locker_api-сьюте для консистентности с прочими shared.database-тестами.
"""
from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from shared.database.config import PostgresConfig


def _valid_kwargs() -> dict[str, object]:
    return {
        "host": "localhost",
        "port": 5432,
        "database": "scud_test",
        "user": "u",
        "password": "p",
    }


def test_valid_config_constructs() -> None:
    cfg = PostgresConfig(**_valid_kwargs())  # type: ignore[arg-type]
    assert cfg.host == "localhost"
    assert cfg.port == 5432
    assert cfg.database == "scud_test"
    assert cfg.connect_timeout_sec == 5
    assert cfg.application_name == "async-postgres-connector"


def test_zero_port_rejected() -> None:
    with pytest.raises(ValueError):
        PostgresConfig(**{**_valid_kwargs(), "port": 0})  # type: ignore[arg-type]


def test_too_large_port_rejected() -> None:
    with pytest.raises(ValueError):
        PostgresConfig(**{**_valid_kwargs(), "port": 70000})  # type: ignore[arg-type]


def test_non_positive_connect_timeout_rejected() -> None:
    with pytest.raises(ValueError):
        PostgresConfig(**{**_valid_kwargs(), "connect_timeout_sec": 0})  # type: ignore[arg-type]


def test_empty_host_rejected() -> None:
    with pytest.raises(ValueError):
        PostgresConfig(**{**_valid_kwargs(), "host": "   "})  # type: ignore[arg-type]


def test_empty_database_rejected() -> None:
    with pytest.raises(ValueError):
        PostgresConfig(**{**_valid_kwargs(), "database": ""})  # type: ignore[arg-type]


def test_empty_user_rejected() -> None:
    with pytest.raises(ValueError):
        PostgresConfig(**{**_valid_kwargs(), "user": ""})  # type: ignore[arg-type]


def test_frozen_attribute_set_raises() -> None:
    cfg = PostgresConfig(**_valid_kwargs())  # type: ignore[arg-type]
    with pytest.raises(FrozenInstanceError):
        cfg.host = "other"  # type: ignore[misc]


def test_connection_reexports_same_config_object() -> None:
    # Фасад реэкспортирует ровно тот же класс, а не копию.
    from shared.database import config as config_mod
    from shared.database.connection import PostgresConfig as ConnPostgresConfig

    assert ConnPostgresConfig is config_mod.PostgresConfig
    assert ConnPostgresConfig is PostgresConfig
