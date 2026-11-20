"""Integration: login-funnel requests drive /metrics counters.

Тест делает реальный HTTP POST /api/v1/auth/login с заведомо неверным
паролем и убеждается, что scud_login_attempts_total{outcome=bad_credential}
на /metrics вырос хотя бы на 1.

Harness: cd backend && docker compose --profile tests run --rm --build auth_api_tests \
             pytest -vv tests/integration/test_login_funnel_metrics.py
"""
from __future__ import annotations

import os
import re

import requests

BASE_URL = os.getenv("TEST_BASE_URL", "http://auth_api:8003")


# ---------------------------------------------------------------------------
# Helpers: metrics parsing
# ---------------------------------------------------------------------------

def _metric_value(text: str, name: str, **labels: str) -> float:
    """Извлечь числовое значение из Prometheus exposition text.

    Возвращает 0.0 если серия с указанными label'ами не найдена.
    Порядок label'ов в тексте не фиксируем — ищем подстроки.
    """
    for line in text.splitlines():
        if not line.startswith(name + "{"):
            continue
        if all(f'{k}="{v}"' in line for k, v in labels.items()):
            m = re.search(r"\s([0-9.eE+\-]+)$", line)
            if m:
                return float(m.group(1))
    return 0.0


def _scrape_metrics() -> str:
    resp = requests.get(f"{BASE_URL}/metrics", timeout=10)
    assert resp.status_code == 200, f"/metrics returned {resp.status_code}"
    return resp.text


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_bad_credential_advances_login_metric(wait_for_api: None) -> None:
    """POST /login с неверным паролем → scud_login_attempts_total на /metrics растёт.

    Использует несуществующий номер телефона, чтобы не зависеть от состояния
    тестового пользователя.

    Если в тестовом стенде IP уже ограничен rate-limit'ом (бывает, когда
    wait_for_api-фикстура сжигает токены во время прогрева сервиса), сервер
    вернёт 429. В обоих случаях login_attempts_total инкрементируется по
    соответствующему outcome (bad_credential или rate_limited), и тест
    проверяет, что суммарный счётчик по оба исхода вырос суммарно.
    """
    before_text = _scrape_metrics()
    before_bad = _metric_value(
        before_text,
        "scud_login_attempts_total",
        service="auth_api",
        outcome="bad_credential",
    )
    before_rate = _metric_value(
        before_text,
        "scud_login_attempts_total",
        service="auth_api",
        outcome="rate_limited",
    )
    before_total = before_bad + before_rate

    resp = requests.post(
        f"{BASE_URL}/api/v1/auth/login",
        json={"phone": "+70000000001", "password": "definitely_wrong"},
        timeout=10,
    )
    # 401 (bad credential) или 429 (rate limited) — оба инкрементируют метрику
    assert resp.status_code in (401, 429), (
        f"Unexpected status for bad credentials: {resp.status_code}: {resp.text}"
    )

    after_text = _scrape_metrics()
    after_bad = _metric_value(
        after_text,
        "scud_login_attempts_total",
        service="auth_api",
        outcome="bad_credential",
    )
    after_rate = _metric_value(
        after_text,
        "scud_login_attempts_total",
        service="auth_api",
        outcome="rate_limited",
    )
    after_total = after_bad + after_rate

    assert after_total - before_total >= 1.0, (
        f"scud_login_attempts_total (bad_credential+rate_limited) did not advance: "
        f"before={before_total} (bad={before_bad}, rate={before_rate}), "
        f"after={after_total} (bad={after_bad}, rate={after_rate})"
    )
