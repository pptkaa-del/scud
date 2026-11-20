"""Unit-тесты: VerifyHandler инкрементирует счётчики метрик Prometheus.

Проверяет, что каждая ветка verify.post() инкрементирует правильный
otp_total{stage=verify, outcome=...}, а success-ветка дополнительно
инкрементирует login_attempts_total{outcome=success_2fa}.
Также проверяет, что в label-значения не попадают секреты (телефон, OTP-код).
"""
from __future__ import annotations

import asyncio
import hashlib
from unittest.mock import AsyncMock, MagicMock

from prometheus_client import REGISTRY

import app.handlers.verify as mod
from app.handlers.verify import VerifyHandler
from shared import metrics
from shared.enums import LoginOutcome, OtpOutcome, OtpStage


def _counter(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _handler(body: dict) -> VerifyHandler:
    """Создать VerifyHandler полностью через MagicMock, без Tornado __init__.

    verify_rate_limit=0 отключает rate-limit ветку (она требует реального db);
    otp_max_attempts=3 соответствует prod-дефолту.

    settings — read-only property от self.application.settings в Tornado,
    поэтому подставляем fake application с нужным dict (см. test_login_metrics.py).
    """
    h = VerifyHandler.__new__(VerifyHandler)
    app = MagicMock()
    app.settings = {
        "verify_rate_limit": 0,
        "otp_max_attempts": 3,
        "service_name": "auth_api",
        "secure_cookies": False,
        "access_token_ttl_hours": 24,
    }
    h.application = app  # type: ignore[assignment]
    h.request = MagicMock(remote_ip="10.0.0.2", headers={})  # type: ignore[assignment]
    h.db = MagicMock()  # type: ignore[assignment]
    h.get_json_body = MagicMock(return_value=body)  # type: ignore[method-assign]
    h.write_validation_error = MagicMock()  # type: ignore[method-assign]
    h.write_unauthorized = MagicMock()  # type: ignore[method-assign]
    h.write_too_many_requests = MagicMock()  # type: ignore[method-assign]
    h.write_json = MagicMock()  # type: ignore[method-assign]
    h.set_status = MagicMock()  # type: ignore[method-assign]
    h.finish = MagicMock()  # type: ignore[method-assign]
    h._set_access_cookie = MagicMock()  # type: ignore[method-assign]
    h._issue_access_token = AsyncMock(return_value="tok")  # type: ignore[method-assign]
    h._get_user_by_phone = AsyncMock(  # type: ignore[method-assign]
        return_value={"user_id": 7, "name": "Иван Иванов", "role": "worker"},
    )
    return h


# ---------------------------------------------------------------------------
# no_active_otp branch
# ---------------------------------------------------------------------------


def test_no_active_otp_increments() -> None:
    before = _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.NO_ACTIVE_OTP.value},
    )
    mod.OtpRepository = MagicMock(  # type: ignore[attr-defined]
        return_value=MagicMock(get_active=AsyncMock(return_value=None)),
    )
    h = _handler({"phone": "x", "code": "123456"})
    asyncio.run(h.post())
    after = _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.NO_ACTIVE_OTP.value},
    )
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# attempts_exhausted branch (reserve_attempt returns None)
# ---------------------------------------------------------------------------


def test_attempts_exhausted_increments() -> None:
    otp_obj = MagicMock(id=1)
    repo = MagicMock(
        get_active=AsyncMock(return_value=otp_obj),
        reserve_attempt=AsyncMock(return_value=None),
    )
    mod.OtpRepository = MagicMock(return_value=repo)  # type: ignore[attr-defined]
    before = _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.ATTEMPTS_EXHAUSTED.value},
    )
    h = _handler({"phone": "x", "code": "123456"})
    asyncio.run(h.post())
    after = _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.ATTEMPTS_EXHAUSTED.value},
    )
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# code_mismatch branch (hash mismatch)
# ---------------------------------------------------------------------------


def test_code_mismatch_increments() -> None:
    otp_obj = MagicMock(id=1)
    repo = MagicMock(
        get_active=AsyncMock(return_value=otp_obj),
        reserve_attempt=AsyncMock(return_value={"code_hash": "correct_hash"}),
    )
    mod.OtpRepository = MagicMock(return_value=repo)  # type: ignore[attr-defined]
    # Ensure hash_otp returns something that won't match "correct_hash"
    mod.hash_otp = lambda c: "wrong_hash"  # type: ignore[attr-defined]
    before = _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.CODE_MISMATCH.value},
    )
    h = _handler({"phone": "x", "code": "123456"})
    asyncio.run(h.post())
    after = _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.CODE_MISMATCH.value},
    )
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# success branch — double-counts otp verify success AND login success_2fa
# ---------------------------------------------------------------------------


def test_verify_success_increments_both() -> None:
    code = "123456"
    code_hash = hashlib.sha256(("salt" + code).encode()).hexdigest()
    # hash_otp patched to return the deterministic code_hash
    mod.hash_otp = lambda c: code_hash  # type: ignore[attr-defined]
    otp_obj = MagicMock(id=1)
    repo = MagicMock(
        get_active=AsyncMock(return_value=otp_obj),
        reserve_attempt=AsyncMock(return_value={"code_hash": code_hash}),
        consume=AsyncMock(return_value=True),
    )
    mod.OtpRepository = MagicMock(return_value=repo)  # type: ignore[attr-defined]
    before_otp = _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.SUCCESS.value},
    )
    before_login = _counter(
        "scud_login_attempts_total",
        {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.SUCCESS_2FA.value},
    )
    h = _handler({"phone": "x", "code": code})
    asyncio.run(h.post())
    assert _counter(
        "scud_otp_total",
        {"service": metrics.SERVICE_NAME, "stage": OtpStage.VERIFY.value,
         "outcome": OtpOutcome.SUCCESS.value},
    ) - before_otp == 1.0
    assert _counter(
        "scud_login_attempts_total",
        {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.SUCCESS_2FA.value},
    ) - before_login == 1.0


# ---------------------------------------------------------------------------
# Security invariant: no secrets in label values
# ---------------------------------------------------------------------------


def test_no_secret_in_label_values() -> None:
    """Проверяет, что label-значения Prometheus не содержат секретных данных."""
    secret_phone = "+79991234567"
    secret_code = "999888"
    secret_token = "tok_very_secret"

    code_hash = hashlib.sha256(("salt" + secret_code).encode()).hexdigest()
    mod.hash_otp = lambda c: code_hash  # type: ignore[attr-defined]
    otp_obj = MagicMock(id=1)
    repo = MagicMock(
        get_active=AsyncMock(return_value=otp_obj),
        reserve_attempt=AsyncMock(return_value={"code_hash": code_hash}),
        consume=AsyncMock(return_value=True),
    )
    mod.OtpRepository = MagicMock(return_value=repo)  # type: ignore[attr-defined]

    h = _handler({"phone": secret_phone, "code": secret_code})
    h._issue_access_token = AsyncMock(return_value=secret_token)  # type: ignore[method-assign]
    asyncio.run(h.post())

    secrets_set = {secret_phone, secret_code, secret_token}
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            for label_val in sample.labels.values():
                assert label_val not in secrets_set, (
                    f"Secret leaked into metric label: {label_val!r} "
                    f"in {metric.name} {sample.labels}"
                )
