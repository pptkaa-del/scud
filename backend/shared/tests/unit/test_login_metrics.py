"""Unit-тесты определений метрик воронки входа (Фаза B2).

Проверяет:
- Корректные значения всех enum-меток (LoginOutcome, OtpStage, OtpOutcome),
  которые ограничивают cardinality новых счётчиков.
- Регистрацию и корректное именование/labeling counter-метрик в Prometheus REGISTRY.

Все label-значения берутся только из соответствующих StrEnum'ов — bare literals
в вызовах .labels() запрещены.
"""
from __future__ import annotations

from prometheus_client import REGISTRY

from shared import metrics
from shared.enums import LoginOutcome, OtpOutcome, OtpStage


def test_login_enums_have_expected_values() -> None:
    assert LoginOutcome.RATE_LIMITED.value == "rate_limited"
    assert LoginOutcome.UPSTREAM_UNAVAILABLE.value == "upstream_unavailable"
    assert LoginOutcome.BAD_CREDENTIAL.value == "bad_credential"
    assert LoginOutcome.SUCCESS_NO_2FA.value == "success_no_2fa"
    assert LoginOutcome.SUCCESS_2FA.value == "success_2fa"
    assert OtpStage.ISSUE.value == "issue"
    assert OtpStage.VERIFY.value == "verify"
    assert {o.value for o in OtpOutcome} >= {
        "sent", "queued", "delivery_unavailable", "rate_limited",
        "no_active_otp", "attempts_exhausted", "code_mismatch", "success",
    }


def test_login_metrics_registered() -> None:
    # Snapshot values BEFORE incrementing — REGISTRY is global and shared across
    # the whole test session, so we cannot rely on absolute values being 1.0.
    before_login = REGISTRY.get_sample_value(
        "scud_login_attempts_total",
        {"service": "t", "outcome": "success_no_2fa"},
    ) or 0.0

    metrics.login_attempts_total.labels(
        service="t", outcome=LoginOutcome.SUCCESS_NO_2FA,
    ).inc()
    metrics.otp_total.labels(
        service="t", stage=OtpStage.ISSUE, outcome=OtpOutcome.SENT,
    ).inc()

    after_login = REGISTRY.get_sample_value(
        "scud_login_attempts_total",
        {"service": "t", "outcome": "success_no_2fa"},
    )
    assert after_login == before_login + 1.0
