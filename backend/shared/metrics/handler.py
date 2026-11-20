"""MetricsHandler — Prometheus scrape endpoint for Tornado services.

Отдаёт текущее значение всех зарегистрированных метрик в exposition
format на ``GET /metrics``. Без аутентификации: внутренний эндпоинт,
доступен только из ``backend_net`` (Apache gateway не проксирует
``/metrics`` наружу).
"""
from __future__ import annotations

import logging

from tornado.web import RequestHandler

from . import (
    CONTENT_TYPE_LATEST,
    generate_latest,
    run_metric_samplers,
    update_pool_gauges,
)

logger = logging.getLogger(__name__)


class MetricsHandler(RequestHandler):
    """``GET /metrics`` — Prometheus exposition format."""

    async def get(self) -> None:
        # Pull-based семплирование gauge'ей в момент scrape. Обёрнуто в
        # try/except: дефект метрик не должен валить весь /metrics-ответ.
        try:
            update_pool_gauges()
        except Exception:
            logger.debug("update_pool_gauges() failed during scrape", exc_info=True)
        try:
            run_metric_samplers()
        except Exception:
            logger.debug("run_metric_samplers() failed during scrape", exc_info=True)
        self.set_header("Content-Type", CONTENT_TYPE_LATEST)
        self.write(generate_latest())

    # Disable authentication path: MetricsHandler наследует чистый
    # RequestHandler, поэтому prepare()/on_finish() из MetricsMixin не
    # подцепляются — это намеренно, чтобы scrape-запрос не появлялся в
    # ``scud_http_requests_total`` (иначе график показывает «активность»
    # даже на простаивающем сервисе).
