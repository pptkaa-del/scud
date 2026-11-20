"""Prometheus-инструментирование HTTP-запросов для базовых хендлеров.

Изолирует устойчивый импорт метрик и сам код записи метрик из
``JSONHandler.on_finish`` в свободную функцию ``record_http_metrics``,
чтобы наблюдаемость не размазывалась по handler-классу.
"""

import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# импорт метрик локально-устойчив — если модуль почему-то не загружен
# (старый Dockerfile без python3-prometheus-client), `_METRICS_AVAILABLE`
# становится False и инструментация полностью отключается. Production-код
# не должен падать из-за инфраструктурного хука.
try:
    from ..metrics import (
        SERVICE_NAME as _METRICS_SERVICE_NAME,
        _normalize_route,
        http_request_duration_seconds,
        http_requests_total,
    )
    _METRICS_AVAILABLE = True
except Exception:
    _METRICS_AVAILABLE = False


def record_http_metrics(handler: Any) -> None:
    """Запись Prometheus-метрик для завершённого HTTP-запроса.

    Вызывается из ``JSONHandler.on_finish``. Любая ошибка внутри глушится —
    наблюдаемость не должна влиять на основной поток обработки.

    Args:
        handler: завершённый Tornado RequestHandler (ожидаются атрибуты
            ``_metrics_start``, ``request``, ``get_status``).
    """
    if not _METRICS_AVAILABLE:
        return

    try:
        start = getattr(handler, "_metrics_start", None)
        duration = time.monotonic() - start if start is not None else 0.0
        route = _normalize_route(handler.request.path or "_unknown")
        method = (handler.request.method or "UNKNOWN").upper()
        status = str(handler.get_status())
        http_requests_total.labels(
            service=_METRICS_SERVICE_NAME,
            method=method,
            route=route,
            status=status,
        ).inc()
        if start is not None:
            http_request_duration_seconds.labels(
                service=_METRICS_SERVICE_NAME,
                method=method,
                route=route,
            ).observe(duration)
    except Exception:
        logger.debug("Failed to record HTTP metrics", exc_info=True)
