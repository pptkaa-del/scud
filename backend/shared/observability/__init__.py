"""Корреляция запросов (X-Request-ID) и структурное JSON-логирование.

Имя пакета `observability` (а не `logging`), чтобы не путать со stdlib
`logging`. Только stdlib — сертифицировано для Astra, без pip.
"""
from __future__ import annotations

from collections.abc import Iterator
import contextlib
import contextvars
from datetime import UTC, datetime
import json
import logging
import os
from typing import Any
from uuid import uuid4

# Носитель корреляции в пределах корутины запроса. Default "" = нет ID.
request_id_var: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default="",
)

# Атрибуты LogRecord, которые НЕ являются extra-полями пользователя.
_STD_LOGRECORD_ATTRS = frozenset(logging.LogRecord(
    "x", logging.INFO, __file__, 0, "", None, None,
).__dict__) | {"message", "asctime", "request_id", "taskName"}


def new_request_id() -> str:
    """Сгенерировать новый correlation-id (uuid4 hex, 32 символа)."""
    return uuid4().hex


def get_request_id() -> str:
    """Текущий correlation-id корутины (или "")."""
    return request_id_var.get()


def set_request_id(value: str) -> contextvars.Token[str]:
    """Установить correlation-id; вернуть токен для reset_request_id()."""
    return request_id_var.set(value)


def reset_request_id(token: contextvars.Token[str]) -> None:
    """Откатить ContextVar к предыдущему значению по токену."""
    request_id_var.reset(token)


@contextlib.contextmanager
def bind_request_id(value: str | None = None) -> Iterator[str]:
    """Контекст-менеджер: установить correlation-id на время блока.

    Генерирует новый, если value is None. По выходу — гарантированный reset
    (для фоновых задач/планировщиков, минующих per-request хуки хендлера).
    """
    rid = value if value is not None else new_request_id()
    token = set_request_id(rid)
    try:
        yield rid
    finally:
        reset_request_id(token)


class RequestIdFilter(logging.Filter):
    """Инжектит `record.request_id` в каждую запись (get_request_id() или "-")."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = get_request_id() or "-"
        return True


class JsonFormatter(logging.Formatter):
    """Форматирует запись в одну JSON-строку.

    Поля: ts, level, service, logger, request_id, message + любые extra,
    переданные через logger.<lvl>(..., extra={...}).
    """

    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        for key, value in record.__dict__.items():
            if key not in _STD_LOGRECORD_ATTRS and not key.startswith("_"):
                payload[key] = value
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure_logging(service_name: str) -> None:
    """Сконфигурировать корневой логгер сервиса.

    Читает LOG_LEVEL (default INFO) и LOG_FORMAT (default json; text —
    человекочитаемый переключатель). Вешает RequestIdFilter и выбранный
    форматтер на единственный StreamHandler корня. Глушит tornado.access
    до WARNING (per-request access-лог пишем сами в JSONHandler.on_finish).
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    log_format = os.getenv("LOG_FORMAT", "json").lower()

    handler = logging.StreamHandler()
    handler.addFilter(RequestIdFilter())
    if log_format == "text":
        handler.setFormatter(logging.Formatter(
            f"%(asctime)s {service_name} %(levelname)s "
            "[%(request_id)s] %(name)s: %(message)s",
        ))
    else:
        handler.setFormatter(JsonFormatter(service_name))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    logging.getLogger("tornado.access").setLevel(logging.WARNING)


__all__ = [
    "JsonFormatter",
    "RequestIdFilter",
    "bind_request_id",
    "configure_logging",
    "get_request_id",
    "new_request_id",
    "request_id_var",
    "reset_request_id",
    "set_request_id",
]
