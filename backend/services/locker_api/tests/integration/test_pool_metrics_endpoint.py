"""Интеграция: /metrics экспортирует DB-pool-saturation gauge'ы.

Греем пул реальным запросом (GET /api/v1/lockers), затем скрейпим /metrics и
проверяем, что присутствуют серии scud_db_pool_connections_{in_use,idle,waiters}
с label'ами service="locker_api", pool="scud_lockers", и значения — числа >= 0.

Pull-based: gauge'и обновляются update_pool_gauges() прямо в MetricsHandler.get()
на каждый scrape, а не из hot-path acquire/release.
"""
from __future__ import annotations

import os
import re

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8001")
SERVICE = "locker_api"
POOL = "scud_lockers"

_METRIC_NAMES = (
    "scud_db_pool_connections_in_use",
    "scud_db_pool_connections_idle",
    "scud_db_pool_connections_waiters",
)


def _parse_metric_value(body: str, metric: str) -> float:
    """Извлечь числовое значение серии metric{...} из exposition-текста.

    prometheus_client сортирует label'ы по алфавиту (`pool` < `service`),
    поэтому порядок в фигурных скобках не фиксируем — проверяем наличие обоих
    нужных label'ов независимо от их позиции.
    """
    pattern = re.compile(
        rf'^{re.escape(metric)}\{{(?P<labels>[^}}]*)\}}\s+(?P<value>[0-9eE.+-]+)$',
        re.MULTILINE,
    )
    for match in pattern.finditer(body):
        labels = match.group("labels")
        if f'service="{SERVICE}"' in labels and f'pool="{POOL}"' in labels:
            return float(match.group("value"))
    raise AssertionError(
        f"серия {metric} с service={SERVICE},pool={POOL} не найдена в /metrics",
    )


def test_metrics_endpoint_exposes_pool_gauges(api_client) -> None:
    # 1. Греем пул: реальный запрос форсирует getconn/putconn.
    warm = api_client.get("/api/v1/lockers")
    assert warm.status_code == 200, warm.text

    # 2. Скрейпим /metrics на порту сервиса (gateway его наружу не проксирует,
    #    но интеграционный клиент бьёт напрямую в locker_api:8001).
    resp = api_client.get("/metrics")
    assert resp.status_code == 200, resp.text
    body = resp.text

    # 3. Каждая из трёх серий присутствует и парсится как число >= 0.
    for metric in _METRIC_NAMES:
        value = _parse_metric_value(body, metric)
        assert value >= 0, f"{metric} вернул отрицательное значение {value}"
