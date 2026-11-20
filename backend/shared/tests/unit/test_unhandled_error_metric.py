"""Unit-тесты scud_unhandled_errors_total.

Проверяем:
- RuntimeError в handler'е → счётчик увеличивается на 1 (unclassified-500).
- UpstreamUnavailableError → счётчик НЕ увеличивается (инфра-деградация, не баг).
- QueryError → счётчик увеличивается (нарушение инварианта — баг кода).
- AsyncPostgresError → счётчик НЕ увеличивается (инфра-деградация БД).

Паттерн ._value.get() / REGISTRY.get_sample_value зеркалит test_login_metrics.py
и test_interservice_duration.py.
"""
from __future__ import annotations

from prometheus_client import REGISTRY
from tornado.testing import AsyncHTTPTestCase
from tornado.web import Application

from shared.auth import ProblemDetailsHandler
from shared.database.errors import AsyncPostgresError, QueryError
from shared.http import UpstreamUnavailableError
from shared.metrics import SERVICE_NAME as _SVC


def _counter_value() -> float:
    """Вернуть текущее значение scud_unhandled_errors_total{service=_SVC}."""
    return (
        REGISTRY.get_sample_value(
            "scud_unhandled_errors_total",
            {"service": _SVC},
        )
        or 0.0
    )


# ---------------------------------------------------------------------------
# Тестовые handler'ы
# ---------------------------------------------------------------------------

class _RuntimeErrorHandler(ProblemDetailsHandler):
    """Имитирует непойманный RuntimeError — unclassified 500, настоящий баг."""

    async def get(self) -> None:
        raise RuntimeError("unexpected internal failure")


class _UpstreamHandler(ProblemDetailsHandler):
    """UpstreamUnavailableError — инфра-деградация, НЕ баг кода."""

    async def get(self) -> None:
        raise UpstreamUnavailableError("upstream down", upstream="svc:8000", status=503)


class _QueryErrorHandler(ProblemDetailsHandler):
    """QueryError — нарушение инварианта, тоже считается багом."""

    async def get(self) -> None:
        raise QueryError("unexpected empty result")


class _AsyncPostgresErrorHandler(ProblemDetailsHandler):
    """AsyncPostgresError — инфра-сбой БД, счётчик НЕ растёт."""

    async def get(self) -> None:
        raise AsyncPostgresError("pool exhausted")


# ---------------------------------------------------------------------------
# Тест
# ---------------------------------------------------------------------------

class UnhandledErrorMetricTest(AsyncHTTPTestCase):
    def get_app(self) -> Application:
        db_kwargs = {"db": None}
        return Application([
            (r"/runtime", _RuntimeErrorHandler, db_kwargs),
            (r"/upstream", _UpstreamHandler, db_kwargs),
            (r"/query", _QueryErrorHandler, db_kwargs),
            (r"/asyncpg", _AsyncPostgresErrorHandler, db_kwargs),
        ])

    def test_runtime_error_increments_counter(self) -> None:
        """Непойманный RuntimeError → scud_unhandled_errors_total += 1."""
        before = _counter_value()
        resp = self.fetch("/runtime")
        assert resp.code == 500, resp.body
        after = _counter_value()
        assert after - before == 1.0, (
            f"Expected counter to increment by 1, got delta={after - before}"
        )

    def test_upstream_unavailable_does_not_increment(self) -> None:
        """UpstreamUnavailableError (503) → счётчик НЕ растёт."""
        before = _counter_value()
        resp = self.fetch("/upstream")
        assert resp.code == 503, resp.body
        after = _counter_value()
        assert after - before == 0.0, (
            f"UpstreamUnavailableError must NOT increment unhandled counter, "
            f"got delta={after - before}"
        )

    def test_query_error_increments_counter(self) -> None:
        """QueryError → scud_unhandled_errors_total += 1 (нарушение инварианта — баг)."""
        before = _counter_value()
        resp = self.fetch("/query")
        assert resp.code == 500, resp.body
        after = _counter_value()
        assert after - before == 1.0, (
            f"QueryError must increment unhandled counter by 1, got delta={after - before}"
        )

    def test_async_postgres_error_does_not_increment(self) -> None:
        """AsyncPostgresError (инфра-сбой БД) → счётчик НЕ растёт."""
        before = _counter_value()
        resp = self.fetch("/asyncpg")
        assert resp.code in (500, 503), resp.body
        after = _counter_value()
        assert after - before == 0.0, (
            f"AsyncPostgresError must NOT increment unhandled counter, "
            f"got delta={after - before}"
        )
