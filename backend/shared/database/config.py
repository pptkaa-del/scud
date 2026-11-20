"""Конфигурация подключения к PostgreSQL.

Frozen+slots dataclass ``PostgresConfig`` с валидацией портов/таймаутов/
непустых строк в ``__post_init__``. Чистый модуль — не импортирует psycopg2,
поэтому его можно использовать там, где драйвер не нужен.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PostgresConfig:
    host: str
    port: int
    database: str
    user: str
    password: str
    connect_timeout_sec: int = 5
    application_name: str = "async-postgres-connector"

    def __post_init__(self) -> None:
        if self.port <= 0 or self.port > 65535:
            raise ValueError(f"Invalid port: {self.port}. Must be 1-65535")
        if self.connect_timeout_sec <= 0:
            raise ValueError(f"Invalid connect_timeout_sec: {self.connect_timeout_sec}. Must be > 0")
        if not self.host.strip():
            raise ValueError("host cannot be empty")
        if not self.database.strip():
            raise ValueError("database cannot be empty")
        if not self.user.strip():
            raise ValueError("user cannot be empty")
