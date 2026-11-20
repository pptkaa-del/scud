"""Unit-тесты для санитизации текста исключений в `ProblemDetailsHandler.write_error`
и `JSONHandler.write_error` (SEC-23).

Проверяем что внутренние детали (Docker-хосты/URL, SQL/драйвер-текст, raw
сообщения багов) НЕ утекают клиенту в `detail`/`title` ProblemDetails, но
по-прежнему логируются на сервере. Дополнительно — что явный dacite/validation
путь с именами полей не задет.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from prometheus_client import REGISTRY
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from shared.auth import JSONHandler, ProblemDetailsHandler
from shared.database.errors import AsyncPostgresError, QueryError, TransactionError
from shared.http import UpstreamUnavailableError
from shared.metrics import SERVICE_NAME as _SVC


def _unhandled_counter() -> float:
    return (
        REGISTRY.get_sample_value("scud_unhandled_errors_total", {"service": _SVC})
        or 0.0
    )

# Маркеры внутренних деталей, которые НИКОГДА не должны утечь клиенту.
INTERNAL_HOST = "user_api:8000"
INTERNAL_URL = "http://user_api:8000/api/v1/internal/check"
RAW_SQL = 'relation "scud_users.secret_table" does not exist'
RAW_PSYCOPG = "psycopg2.errors.UndefinedColumn: column foo does not exist"
RAW_VALUE_BUG = "invalid literal for int() with base 10: 'oops'"


@dataclass
class _Dto:
    name: str
    count: int


class _UpstreamHandler(ProblemDetailsHandler):
    async def get(self) -> None:
        raise UpstreamUnavailableError(
            f"503 from {INTERNAL_URL}", upstream=INTERNAL_HOST, status=503,
        )


class _PostgresHandler(ProblemDetailsHandler):
    async def get(self) -> None:
        raise AsyncPostgresError(f"connection failed: {RAW_PSYCOPG}")


class _TransactionHandler(ProblemDetailsHandler):
    async def get(self) -> None:
        raise TransactionError(f"BEGIN failed: {RAW_SQL}")


class _QueryHandler(ProblemDetailsHandler):
    async def get(self) -> None:
        raise QueryError(f"invariant broken: {RAW_SQL}")


class _ValueErrorHandler(ProblemDetailsHandler):
    async def get(self) -> None:
        # Имитация глубокого бага: int() падает где-то внутри handler'а.
        raise ValueError(RAW_VALUE_BUG)


class _DaciteValidationHandler(ProblemDetailsHandler):
    async def post(self) -> None:
        # Явный путь валидации через dacite — поля по имени, это НЕ утечка.
        dto = self.get_json_body_as(_Dto)
        self.write_json({"name": dto.name, "count": dto.count})


class _LegacyPostgresHandler(JSONHandler):
    async def get(self) -> None:
        raise AsyncPostgresError(f"connection failed: {RAW_PSYCOPG}")


class _LegacyValueErrorHandler(JSONHandler):
    async def get(self) -> None:
        raise ValueError(RAW_VALUE_BUG)


class WriteErrorLeakTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        # db не нужен этим handler'ам (они кидают исключение до любого
        # обращения к БД) — передаём None через route kwargs.
        db_kwargs = {"db": None}
        return Application([
            (r"/upstream", _UpstreamHandler, db_kwargs),
            (r"/postgres", _PostgresHandler, db_kwargs),
            (r"/transaction", _TransactionHandler, db_kwargs),
            (r"/query", _QueryHandler, db_kwargs),
            (r"/valueerror", _ValueErrorHandler, db_kwargs),
            (r"/dacite", _DaciteValidationHandler, db_kwargs),
            (r"/legacy-postgres", _LegacyPostgresHandler, db_kwargs),
            (r"/legacy-valueerror", _LegacyValueErrorHandler, db_kwargs),
        ])

    # --- helpers ---------------------------------------------------------

    def _body_text(self, path: str, method: str = "GET", body: bytes | None = None) -> tuple[int, str]:
        resp = (
            self.fetch(path, method=method, body=body)
            if body is not None
            else self.fetch(path, method=method)
        )
        return resp.code, resp.body.decode("utf-8")

    # --- UpstreamUnavailable: no hostname/URL leak -----------------------

    def test_upstream_unavailable_hides_internal_host(self) -> None:
        before = _unhandled_counter()
        with self.assertLogs("shared.auth.problem_details", level="ERROR") as cm:
            code, text = self._body_text("/upstream")
        assert code == 503, text
        # КЛИЕНТУ: ни хоста, ни URL, ни схемы.
        assert INTERNAL_HOST not in text, f"leaked host: {text}"
        assert INTERNAL_URL not in text, f"leaked url: {text}"
        assert "http://" not in text, f"leaked scheme: {text}"
        # Структура RFC 7807 сохранена + generic detail.
        problem = json.loads(text)
        assert problem["status"] == 503
        assert problem["detail"]  # есть человекочитаемый текст
        # СЕРВЕРУ: полный текст в логах.
        logged = "\n".join(cm.output)
        assert INTERNAL_HOST in logged
        assert INTERNAL_URL in logged
        # Инфра-деградация — счётчик багов НЕ растёт.
        assert _unhandled_counter() - before == 0.0, "UpstreamUnavailableError must NOT increment unhandled counter"

    # --- AsyncPostgresError / Transaction: no SQL/driver leak ------------

    def test_postgres_error_hides_driver_text(self) -> None:
        with self.assertLogs("shared.auth.problem_details", level="ERROR") as cm:
            code, text = self._body_text("/postgres")
        assert code in (500, 503), text
        assert RAW_PSYCOPG not in text, f"leaked driver text: {text}"
        assert "psycopg2" not in text, f"leaked driver name: {text}"
        problem = json.loads(text)
        assert problem["detail"]
        assert RAW_PSYCOPG in "\n".join(cm.output)

    def test_transaction_error_hides_sql(self) -> None:
        with self.assertLogs("shared.auth.problem_details", level="ERROR") as cm:
            code, text = self._body_text("/transaction")
        assert code in (500, 503), text
        assert RAW_SQL not in text, f"leaked SQL: {text}"
        assert "scud_users.secret_table" not in text
        assert RAW_SQL in "\n".join(cm.output)

    def test_query_error_hides_sql(self) -> None:
        with self.assertLogs("shared.auth.problem_details", level="ERROR") as cm:
            code, text = self._body_text("/query")
        assert code == 500, text
        # Маркер без кавычек (кавычки JSON-эскейпятся, дают ложный pass).
        assert "scud_users.secret_table" not in text, f"leaked SQL: {text}"
        assert "invariant broken" not in text, f"leaked raw text: {text}"
        assert RAW_SQL in "\n".join(cm.output)

    # --- bare ValueError: generic, logged, NOT raw -----------------------

    def test_bare_value_error_does_not_leak_raw_message(self) -> None:
        before = _unhandled_counter()
        with self.assertLogs("shared.auth.problem_details", level="ERROR") as cm:
            code, text = self._body_text("/valueerror")
        # Дизайн (a): bare ValueError -> generic 500 (логируется), не 400-маскарад.
        assert code == 500, text
        assert RAW_VALUE_BUG not in text, f"leaked raw bug message: {text}"
        assert "int()" not in text
        assert RAW_VALUE_BUG in "\n".join(cm.output)
        # ValueError — баг кода → счётчик должен вырасти.
        assert _unhandled_counter() - before == 1.0, "ValueError must increment unhandled counter"

    # --- explicit dacite/validation 400 path is UNAFFECTED ---------------

    def test_dacite_validation_400_still_field_named(self) -> None:
        # Невалидное тело: count не целое -> dacite WrongTypeError по полю.
        code, text = self._body_text(
            "/dacite", method="POST", body=b'{"name": "x", "count": "notint"}',
        )
        assert code == 400, text
        problem = json.loads(text)
        assert problem["status"] == 400
        # Имя поля присутствует — explicit validation путь работает.
        assert "count" in problem["detail"], problem["detail"]

    def test_dacite_happy_path_unaffected(self) -> None:
        code, text = self._body_text(
            "/dacite", method="POST", body=b'{"name": "ok", "count": 3}',
        )
        assert code == 200, text
        assert json.loads(text) == {"name": "ok", "count": 3}

    # --- legacy JSONHandler funnel также санитизирован -------------------

    def test_legacy_postgres_error_hides_driver_text(self) -> None:
        with self.assertLogs("shared.auth.json_handler", level="ERROR") as cm:
            code, text = self._body_text("/legacy-postgres")
        assert code in (500, 503), text
        assert RAW_PSYCOPG not in text, f"leaked driver text: {text}"
        assert RAW_PSYCOPG in "\n".join(cm.output)

    def test_legacy_bare_value_error_does_not_leak_raw(self) -> None:
        with self.assertLogs("shared.auth.json_handler", level="ERROR") as cm:
            code, text = self._body_text("/legacy-valueerror")
        assert code == 500, text
        assert RAW_VALUE_BUG not in text, f"leaked raw bug message: {text}"
        assert RAW_VALUE_BUG in "\n".join(cm.output)


# Совместимость с pytest без unittest-раннера: классы AsyncHTTPTestCase
# подхватываются pytest автоматически. logging-уровень root не глушим.
logging.getLogger("shared.auth.json_handler").setLevel(logging.DEBUG)
logging.getLogger("shared.auth.problem_details").setLevel(logging.DEBUG)
