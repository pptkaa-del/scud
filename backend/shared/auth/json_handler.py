"""Базовый Tornado-хендлер с поддержкой JSON (`JSONHandler`).

Предоставляет общую функциональность для всех handlers:
- Сериализация/десериализация JSON
- Базовая обработка ошибок (санитизация internal-failure текста, SEC-23)
- Работа с БД
- Автоматический сбор Prometheus-метрик HTTP-запросов
"""

from dataclasses import asdict
import json
import logging
import time
from typing import Any

from tornado.web import Finish, RequestHandler

from ..database import AsyncPostgres
from ..database.errors import AsyncPostgresError, QueryError
from ..metrics import inc_unhandled_error
from ..observability import (
    get_request_id,
    new_request_id,
    request_id_var,
    reset_request_id,
    set_request_id,
)
from ._constants import (
    DETAIL_DATABASE_ERROR,
    DETAIL_DATABASE_UNAVAILABLE,
    DETAIL_INTERNAL_ERROR,
)
from .instrumentation import record_http_metrics

logger = logging.getLogger(__name__)


class JSONHandler(RequestHandler):
    """
    Базовый handler с поддержкой JSON.

    Предоставляет утилиты для работы с JSON и базовую обработку ошибок.
    Сервисы могут наследоваться от этого класса и добавлять свою логику авторизации.
    """

    db: AsyncPostgres
    request_id: str = ""

    def initialize(self, db: AsyncPostgres) -> None:
        """
        Инициализация handler с БД.

        Args:
            db: Инстанс AsyncPostgres для работы с БД
        """
        self.db = db

    async def prepare(self) -> None:
        """Подготовка запроса (вызывается перед HTTP методом)"""
        logger.debug(f"JSONHandler.prepare() for {self.request.method} {self.request.uri}")
        # X-Request-ID: берём от шлюза/клиента; если пуст (прямой вызов,
        # тест, internal-endpoint мимо шлюза) — генерируем. Кладём в ContextVar
        # (для логов/inter-service) и в self.request_id (для эхо в write_json).
        incoming = self.request.headers.get("X-Request-ID", "").strip()
        self.request_id = incoming or new_request_id()
        self._request_id_token = set_request_id(self.request_id)
        # фиксируем старт запроса для http_request_duration_seconds.
        # Атрибут на instance, чтобы on_finish мог посчитать длительность
        # даже если prepare() из миксина выкинул Finish (auth fail).
        self._metrics_start = time.monotonic()
        # Вызываем super().prepare() без await, т.к. это может быть либо
        # синхронный RequestHandler.prepare(), либо async prepare() из миксинов
        result = super().prepare()
        # Если результат - корутина, то await её
        if result is not None:
            await result

    def on_finish(self) -> None:
        """Запись Prometheus-метрик и структурного access-лога.

        Вызывается Tornado'ом ВСЕГДА (успех / ошибка / Finish). Любая
        ошибка внутри глушится — наблюдаемость не должна влиять на
        основной поток обработки. В конце сбрасываем request-id ContextVar,
        чтобы он не утёк между корутинами пула обработчиков.
        """
        try:
            super().on_finish()
        except Exception:
            logger.debug("super().on_finish() raised", exc_info=True)

        record_http_metrics(self)

        try:
            start = getattr(self, "_metrics_start", None)
            duration_ms = (
                round((time.monotonic() - start) * 1000, 1) if start is not None else None
            )
            logger.info(
                "request",
                extra={
                    "method": self.request.method,
                    "route": self.request.path,
                    "status": self.get_status(),
                    "duration_ms": duration_ms,
                },
            )
        except Exception:
            logger.debug("Failed to emit access log", exc_info=True)

        token = getattr(self, "_request_id_token", None)
        if token is not None:
            try:
                reset_request_id(token)
            except Exception:
                request_id_var.set("")

    def set_default_headers(self) -> None:
        """Устанавливает заголовки по умолчанию для ВСЕХ ответов (включая ошибки).

        Tornado вызывает ``set_default_headers()`` как при инициализации
        handler'а (``__init__`` → ``clear()``), так и из ``send_error`` /
        ``write_error`` (через внутренний ``clear()``), что позволяет
        эхировать ``X-Request-ID`` на всех путях — успех и ошибка.

        Атрибут ``request_id`` имеет значение ``""`` на уровне класса
        (добавлено в A2), поэтому вызов при инициализации (до ``prepare()``)
        безопасен: ``getattr`` вернёт ``""`` и заголовок не выставится.
        После ``prepare()`` ``self.request_id`` уже заполнен — заголовок
        проставляется и сохраняется через ``clear()`` → ``write_error``.
        """
        super().set_default_headers()
        rid = getattr(self, "request_id", "")
        if rid:
            self.set_header("X-Request-ID", rid)

    def write_json(self, data: Any, status_code: int = 200) -> None:
        """
        Отправляет JSON ответ клиенту.

        Args:
            data: Данные для сериализации в JSON
            status_code: HTTP статус код
        """
        self.set_status(status_code)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        rid = getattr(self, "request_id", "") or get_request_id()
        if rid:
            self.set_header("X-Request-ID", rid)
        self.write(json.dumps(data, default=self._json_serializer))

    def _json_serializer(self, obj: Any) -> Any:
        """
        Кастомный сериализатор для JSON.

        Поддерживает:
        - Dataclasses (через asdict)
        - datetime/date объекты (через isoformat)
        - Прочие объекты (через str())

        Args:
            obj: Объект для сериализации

        Returns:
            Сериализованное представление объекта
        """
        if hasattr(obj, "__dataclass_fields__"):
            return asdict(obj)
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return str(obj)

    def get_json_body(self) -> dict[str, Any]:
        """
        Парсит тело запроса как JSON.

        При запуске под ProblemDetailsHandler (наследник предоставляет
        write_validation_error) парсинг non-dict body или невалидного JSON
        отдаёт клиенту 400 и поднимает Finish — это loud-failure, чтобы
        каскадные `MissingValueError` от dacite не маскировали реальную
        проблему "тело пришло не объектом". Для legacy/standalone-юзеров
        JSONHandler возвращает {} (старое поведение).

        Returns:
            Словарь с данными из body или пустой словарь при ошибке.
        """
        try:
            if not self.request.body:
                return {}
            parsed = json.loads(self.request.body)
        except (ValueError, TypeError) as e:
            logger.warning(f"Failed to parse request body as JSON: {e}")
            if hasattr(self, "write_validation_error"):
                self.write_validation_error("Invalid JSON body")
                raise Finish from e
            return {}
        if not isinstance(parsed, dict):
            logger.warning(
                "Request body is not a JSON object: %s", type(parsed).__name__,
            )
            if hasattr(self, "write_validation_error"):
                self.write_validation_error("Request body must be a JSON object")
                raise Finish
            return {}
        return parsed

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        """
        Обрабатывает ошибки и возвращает JSON ответ.

        Internal-failure классы отдают клиенту ТОЛЬКО generic-сообщение
        (без хостов/URL/SQL/драйвер-текста), полный текст пишется в
        серверный лог (SEC-23):
        - AsyncPostgresError -> 503/500, generic detail, лог с реальным текстом
        - QueryError -> 500, generic detail, лог
        - bare ValueError -> 500 (это баг кода, а не валидация ввода — явная
          валидация идёт через write_validation_error/get_json_body_as)
        - Прочие -> используется переданный status_code

        Args:
            status_code: HTTP статус код
            **kwargs: Дополнительные аргументы (включая exc_info)
        """
        self.set_header("Content-Type", "application/json; charset=utf-8")

        if "exc_info" in kwargs:
            _exc_type, e, _tb = kwargs["exc_info"]

            # Инфра-сбои БД (пул соединений, retry, transaction lock) — 503,
            # клиенту имеет смысл повторить. AsyncPostgresError — sibling
            # QueryError'у, оба наследники DatabaseError. Реальный текст
            # (psycopg2/SQL) — только в лог, клиенту generic.
            if isinstance(e, AsyncPostgresError):
                logger.error("Database error: %s", e, exc_info=True)
                self.set_status(503)
                self.finish(json.dumps({
                    "error": "Database error",
                    "details": DETAIL_DATABASE_UNAVAILABLE,
                }))
                return

            # Ошибки самого запроса/инвариантов (UnexpectedEmptyResultError
            # и потомки QueryError) — 500: проблема в коде, retry не поможет,
            # клиенту нужно репортить в мониторинг (структурный лог level=ERROR; Zabbix-триггер по счётчику).
            if isinstance(e, QueryError):
                logger.error("Query invariant violation: %s", e, exc_info=True)
                inc_unhandled_error()
                self.set_status(500)
                self.finish(json.dumps({
                    "error": "Query invariant violation",
                    "details": DETAIL_DATABASE_ERROR,
                }))
                return

            # bare ValueError — это НЕ пользовательская валидация (та идёт
            # явным путём через write_validation_error/get_json_body_as,
            # минуя write_error за счёт tornado.web.Finish). Любой ValueError,
            # долетевший сюда, — это баг кода: 500 + generic, текст в лог,
            # чтобы баг пейджил on-call, а не маскировался под 400.
            if isinstance(e, ValueError):
                logger.error("Unhandled ValueError in handler: %s", e, exc_info=True)
                inc_unhandled_error()
                self.set_status(500)
                self.finish(json.dumps({
                    "error": "Internal server error",
                    "details": DETAIL_INTERNAL_ERROR,
                }))
                return

            # Неклассифицированное исключение — настоящий баг, не попавший ни в одну ветку.
            inc_unhandled_error()

        # Общая ошибка
        message = self._reason
        self.finish(json.dumps({"error": message}))
