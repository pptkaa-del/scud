"""Базовый Tornado-хендлер с поддержкой RFC 7807 Problem Details.

Расширяет ``JSONHandler`` стандартным набором ``write_*`` хелперов,
формирующих ответы в формате ``application/problem+json``, плюс утилитами
парсинга/валидации path/query/body-аргументов.
"""

from dataclasses import asdict, replace
import json
import logging
from typing import Any
import uuid

from dacite import Config, from_dict
from dacite.exceptions import MissingValueError, UnexpectedDataError, WrongTypeError
from tornado.web import Finish

from ..database.errors import AsyncPostgresError, QueryError
from ..http import (
    STATUS_TO_PROBLEM,
    ProblemDetails,
    ProblemTitle,
    ProblemType,
    UpstreamUnavailableError,
)
from ..metrics import inc_unhandled_error
from ._constants import (
    DETAIL_DATABASE_ERROR,
    DETAIL_DATABASE_UNAVAILABLE,
    DETAIL_INTERNAL_ERROR,
    DETAIL_UPSTREAM_UNAVAILABLE,
    INT_ARG_ERROR_TEMPLATE,
    E,
    T,
)
from .json_handler import JSONHandler

logger = logging.getLogger(__name__)


class ProblemDetailsHandler(JSONHandler):
    """
    Базовый handler с поддержкой RFC 7807 Problem Details.

    Расширяет JSONHandler стандартным набором `write_*` хелперов,
    формирующих ответы в формате `application/problem+json`.

    Сервисы должны наследоваться от этого класса (либо напрямую,
    либо через сервисные миксины) вместо переопределения тех же
    методов локально.

    Спецификация: https://datatracker.ietf.org/doc/html/rfc7807
    """

    def write_problem(
        self,
        type_uri: str,
        title: str,
        status: int,
        detail: str,
        instance: str | None = None,
    ) -> None:
        """RFC 7807 Problem Details response."""
        problem = ProblemDetails(
            type=type_uri,
            title=title,
            status=status,
            detail=detail,
            instance=instance or self.request.uri,
        )
        self.set_status(status)
        self.set_header("Content-Type", "application/problem+json; charset=utf-8")
        rid = getattr(self, "request_id", "")
        if rid:
            self.set_header("X-Request-ID", rid)
        self.write(json.dumps(asdict(problem)))

    def write_validation_error(self, detail: str) -> None:
        """400 Bad Request — ошибка валидации входных данных."""
        self.write_problem(
            ProblemType.VALIDATION_ERROR, ProblemTitle.VALIDATION_ERROR, 400, detail
        )

    def write_invalid_json(self, detail: str) -> None:
        """400 Bad Request — некорректный JSON в теле запроса."""
        self.write_problem(
            ProblemType.INVALID_JSON, ProblemTitle.INVALID_JSON, 400, detail
        )

    def write_unauthorized(self, detail: str) -> None:
        """401 Unauthorized — ошибка аутентификации."""
        self.write_problem(
            ProblemType.UNAUTHORIZED, ProblemTitle.UNAUTHORIZED, 401, detail
        )

    def write_forbidden(self, detail: str) -> None:
        """403 Forbidden — отсутствуют права доступа."""
        self.write_problem(
            ProblemType.FORBIDDEN, ProblemTitle.FORBIDDEN, 403, detail
        )

    def write_not_found(self, detail: str) -> None:
        """404 Not Found — ресурс не найден."""
        self.write_problem(
            ProblemType.NOT_FOUND, ProblemTitle.NOT_FOUND, 404, detail
        )

    def write_conflict(self, detail: str) -> None:
        """409 Conflict — конфликт состояния ресурса."""
        self.write_problem(
            ProblemType.CONFLICT, ProblemTitle.CONFLICT, 409, detail
        )

    def write_payload_too_large(self, detail: str) -> None:
        """413 Payload Too Large — тело запроса превышает допустимый размер."""
        self.write_problem(
            ProblemType.PAYLOAD_TOO_LARGE, ProblemTitle.PAYLOAD_TOO_LARGE, 413, detail
        )

    def write_unsupported_media_type(self, detail: str) -> None:
        """415 Unsupported Media Type — Content-Type запроса не поддерживается."""
        self.write_problem(
            ProblemType.UNSUPPORTED_MEDIA_TYPE,
            ProblemTitle.UNSUPPORTED_MEDIA_TYPE,
            415,
            detail,
        )

    def write_too_many_requests(self, detail: str) -> None:
        """429 Too Many Requests — превышен rate limit."""
        self.write_problem(
            ProblemType.TOO_MANY_REQUESTS, ProblemTitle.TOO_MANY_REQUESTS, 429, detail
        )

    def write_internal_error(self, detail: str) -> None:
        """500 Internal Server Error — непредвиденная ошибка сервера."""
        self.write_problem(
            ProblemType.INTERNAL_ERROR, ProblemTitle.INTERNAL_ERROR, 500, detail
        )

    def write_database_error(self, detail: str) -> None:
        """500 Internal Server Error — ошибка работы с БД."""
        self.write_problem(
            ProblemType.DATABASE_ERROR, ProblemTitle.DATABASE_ERROR, 500, detail
        )

    def write_service_unavailable(self, detail: str) -> None:
        """503 Service Unavailable — upstream-сервис недоступен."""
        self.write_problem(
            ProblemType.SERVICE_UNAVAILABLE,
            ProblemTitle.SERVICE_UNAVAILABLE,
            503,
            detail,
        )

    def write_error(self, status_code: int, **kwargs: Any) -> None:
        """RFC 7807-совместимый обработчик ошибок.

        Перекрывает JSONHandler.write_error: вместо `{error, details}` отдаём
        полноценный problem+json через write_*-хелперы. Это важно для
        UnexpectedEmptyResultError / QueryError, которые иначе уходили бы в
        Tornado default → 500 c HTML-телом, ломая RFC 7807 контракт API.
        """
        if "exc_info" in kwargs:
            _exc_type, e, _tb = kwargs["exc_info"]

            # Upstream HTTP лёг — 503; детали upstream ушли в структурный лог (SEC-23);
            # Zabbix триггерит по росту scud_http_requests_total{status="503"}.
            # ВАЖНО (SEC-23): e.upstream — внутренний Docker-хост (user_api:8000),
            # str(e) часто несёт полный internal URL. Клиенту отдаём generic,
            # реальный хост/URL — только в серверный лог.
            if isinstance(e, UpstreamUnavailableError):
                logger.error("Upstream %s unavailable: %s", e.upstream, e, exc_info=True)
                self.write_service_unavailable(DETAIL_UPSTREAM_UNAVAILABLE)
                return

            # Инфра-сбой БД — реальный текст (psycopg2/SQL/TransactionError)
            # только в лог, клиенту generic 503.
            if isinstance(e, AsyncPostgresError):
                logger.error("Database error: %s", e, exc_info=True)
                self.write_service_unavailable(DETAIL_DATABASE_UNAVAILABLE)
                return

            # QueryError — нарушение инварианта, retry не поможет. SQL/текст
            # запроса — только в лог.
            if isinstance(e, QueryError):
                logger.error("Query invariant violation: %s", e, exc_info=True)
                inc_unhandled_error()
                self.write_database_error(DETAIL_DATABASE_ERROR)
                return

            # bare ValueError — это НЕ пользовательская валидация. Явная
            # валидация ввода идёт через write_validation_error /
            # get_json_body_as (dacite), которые сами шлют 400 с именами полей
            # и поднимают tornado.web.Finish, НЕ доходя до write_error. Любой
            # ValueError, долетевший сюда, — баг кода: 500 generic + лог, чтобы
            # пейджить on-call, а не маскировать под пользовательскую 400.
            if isinstance(e, ValueError):
                logger.error("Unhandled ValueError in handler: %s", e, exc_info=True)
                inc_unhandled_error()
                self.write_internal_error(DETAIL_INTERNAL_ERROR)
                return

            # Неклассифицированное исключение (RuntimeError, AttributeError и т.п.) —
            # настоящий баг, не попавший ни в одну ветку выше.
            inc_unhandled_error()

        # Fallback: status_code от Tornado без exc_info (404 от роутера,
        # 405 от method_not_allowed и т.п.) — мапим в правильный ProblemType,
        # чтобы 404 не выглядел как INTERNAL_ERROR (нарушение RFC 7807).
        type_uri, title = STATUS_TO_PROBLEM.get(
            status_code, (ProblemType.INTERNAL_ERROR, ProblemTitle.INTERNAL_ERROR),
        )
        self.write_problem(
            type_uri,
            title,
            status_code or 500,
            self._reason or "Internal server error",
        )

    def parse_pagination_params(
        self,
        default_limit: int = 100,
        max_limit: int = 1000,
    ) -> tuple[int, int] | None:
        """
        Извлекает и валидирует query-параметры `limit`/`offset`.

        Возвращает `(limit, offset)` либо `None`, если параметры некорректны
        (в этом случае метод сам отправляет 400 через `write_validation_error`,
        вызывающему handler'у нужно лишь сделать `return`).

        Args:
            default_limit: значение `limit` если не передан в query.
            max_limit: верхняя граница `limit` (1 ≤ limit ≤ max_limit).

        Returns:
            (limit, offset) при успехе; None при ошибке валидации.
        """
        try:
            limit_str = self.get_query_argument("limit", default=str(default_limit)) or str(default_limit)
            offset_str = self.get_query_argument("offset", default="0") or "0"
            limit = int(limit_str)
            offset = int(offset_str)
        except ValueError:
            self.write_validation_error("limit and offset must be integers")
            return None

        if limit < 1 or limit > max_limit:
            self.write_validation_error(
                f"limit must be between 1 and {max_limit}"
            )
            return None
        if offset < 0:
            self.write_validation_error("offset must be >= 0")
            return None

        return limit, offset

    # Алиас под старое имя метода, чтобы не ломать существующие вызовы.
    write_error_response = write_problem

    def parse_uuid_or_404(self, value: str, what: str = "resource") -> uuid.UUID:
        """Распарсить path-сегмент как UUID либо отдать 404 и прервать handler.

        Невалидный UUID в path-сегменте (`/locker/{id}`) — это запрос к
        несуществующему ресурсу, а не серверная ошибка: маппим в 404, а не в
        500 (raw `ValueError` от `uuid.UUID()` иначе долетел бы до write_error).
        Метод сам пишет Problem Details и поднимает ``tornado.web.Finish`` —
        вызывающему handler'у возвращать ничего не нужно.

        Возвращает разобранный ``uuid.UUID`` для вызывающих, которым нужна
        нормализованная форма; использовать как чистый guard (без сохранения
        результата) — штатный сценарий: PG-колонки типа ``uuid`` сопоставляют
        регистронезависимо, поэтому исходную строку можно передавать в репозиторий.
        """
        try:
            return uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as e:
            self.write_not_found(f"{what} not found")
            if not self._finished:
                self.finish()
            raise Finish from e

    def parse_enum_or_400(
        self,
        value: str,
        enum_class: type[E],
        field: str,
    ) -> E:
        """Распарсить значение фильтра/поля как член enum либо отдать 400.

        Невалидное значение enum в query-фильтре или path — это ошибка ввода
        (400), а не серверная ошибка (500): без явной проверки строка уходит в
        SQL по enum-колонке и psycopg2 поднимает 22P02, которая маскируется под
        500. Метод сам пишет 400 Problem Details и поднимает ``Finish``.
        """
        try:
            return enum_class(value)
        except ValueError as e:
            allowed = ", ".join(str(member.value) for member in enum_class)
            self.write_validation_error(
                f"Invalid {field} value: {value!r}. Allowed: {allowed}"
            )
            if not self._finished:
                self.finish()
            raise Finish from e

    def get_int_path_arg(self, value: str, field: str) -> int:
        """Распарсить path/строковый аргумент как ``int`` либо отдать 400.

        Дедуплицирует повторяющийся блок «взять path-сегмент, ``int()`` его,
        при ``ValueError`` отправить 400 Problem Details и прервать handler».
        Невалидное целое в path/query — ошибка ввода (400), а не серверная.
        Метод сам пишет 400 ``application/problem+json`` (через
        ``write_validation_error``), вызывает ``self.finish()`` и поднимает
        ``tornado.web.Finish`` — вызывающему handler'у возвращать ничего не
        нужно (как у ``parse_uuid_or_404`` / ``parse_enum_or_400``).

        Сырое ``value`` НЕ эхо-ится в detail — сообщение формируется только из
        ``field`` + generic-текста, чтобы не отражать потенциально вредоносный
        ввод обратно клиенту.

        Args:
            value: исходная строка аргумента (обычно path-сегмент из сигнатуры
                метода handler'а, напр. ``async def get(self, user_id: str)``).
            field: имя поля для сообщения об ошибке (``user_id``, ``event_id``).

        Returns:
            Разобранное целое при успехе.
        """
        try:
            return int(value)
        except (ValueError, TypeError) as e:
            self.write_validation_error(INT_ARG_ERROR_TEMPLATE.format(field=field))
            if not self._finished:
                self.finish()
            raise Finish from e

    def get_json_body_as(
        self,
        dto_class: type[T],
        config: Config | None = None,
        strict: bool = True,
    ) -> T:
        """Распарсить тело запроса в DTO (dataclass) через dacite.

        При ошибке валидации/парсинга метод сам отправляет 400
        `application/problem+json` (через ``write_validation_error``),
        вызывает ``self.finish()`` для коммита ответа и кидает
        ``tornado.web.Finish`` для прерывания handler'а. Tornado обрабатывает
        ``Finish`` особым случаем (без `clear`/`write_error`), так что наш
        Problem Details body доезжает до клиента в неизменном виде.

        ``strict=True`` (по умолчанию для входящих тел): неизвестные ключи в
        теле запроса отвергаются 400 (VAL-03), а не молча отбрасываются — иначе
        опечатка в имени поля (``max_retires``) тихо подставляла бы default.
        """
        body_dict = self.get_json_body()
        effective_config = config or Config()
        if strict and not effective_config.strict:
            # Config — обычный dataclass с cached_property; не мутируем
            # переданный инстанс (он часто модульно-разделяемый _DTO_CONFIG),
            # а делаем копию со strict=True.
            effective_config = replace(effective_config, strict=True)
        try:
            return from_dict(dto_class, body_dict, config=effective_config)
        except (
            KeyError,
            TypeError,
            ValueError,
            MissingValueError,
            WrongTypeError,
            UnexpectedDataError,
        ) as e:
            logger.error("Failed to parse body as %s: %s", dto_class.__name__, e)
            self.write_validation_error(str(e))
            # Закрываем ответ явно: если в handler'е стоит broad
            # ``except Exception`` без специальной ветки на Finish, он попытается
            # записать internal_error, но `_finished=True` сделает запись no-op,
            # и клиент всё равно получит 400 + Problem Details.
            if not self._finished:
                self.finish()
            raise Finish from e
