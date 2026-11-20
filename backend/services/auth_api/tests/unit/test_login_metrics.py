"""Unit-тесты: LoginHandler инкрементирует счётчики метрик Prometheus.

Проверяет, что каждая ветка login.post() инкрементирует правильный outcome/stage,
а в label'ах не оказывается телефон, пароль, OTP-код или другой секретный материал.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from prometheus_client import REGISTRY

from app.handlers.login import LoginHandler
from app.services.notification_client import OtpDeliveryStatus
from shared import metrics
from shared.enums import LoginOutcome, OtpOutcome, OtpStage
from shared.http import UpstreamUnavailableError


def _counter(name: str, labels: dict) -> float:
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _make_handler(
    body: dict,
    *,
    check_result=None,
    check_raises: Exception | None = None,
    otp_status: OtpDeliveryStatus | None = None,
    otp_raises: Exception | None = None,
) -> LoginHandler:
    """Создать LoginHandler полностью через MagicMock, без Tornado __init__."""
    h = LoginHandler.__new__(LoginHandler)
    # LoginHandler.settings — read-only property, читающее self.application.settings.
    app = MagicMock()
    app.settings = {"service_name": "auth_api", "login_rate_limit": 10, "login_rate_window_seconds": 60}
    h.application = app  # type: ignore[assignment]
    h.request = MagicMock()
    h.request.remote_ip = "10.0.0.1"
    h.db = MagicMock()
    # Мокируем write_* методы, чтобы не зависеть от Tornado-буферов.
    h.get_json_body = MagicMock(return_value=body)  # type: ignore[method-assign]
    h.write_too_many_requests = MagicMock()  # type: ignore[method-assign]
    h.write_validation_error = MagicMock()  # type: ignore[method-assign]
    h.write_service_unavailable = MagicMock()  # type: ignore[method-assign]
    h.write_unauthorized = MagicMock()  # type: ignore[method-assign]
    h.write_json = MagicMock()  # type: ignore[method-assign]
    h.write_problem = MagicMock()  # type: ignore[method-assign]
    h._set_access_cookie = MagicMock()  # type: ignore[method-assign]
    h._issue_access_token = AsyncMock(return_value="tok")  # type: ignore[method-assign]
    if check_raises is not None:
        h._check_credentials = AsyncMock(side_effect=check_raises)  # type: ignore[method-assign]
    else:
        h._check_credentials = AsyncMock(return_value=check_result)  # type: ignore[method-assign]
    if otp_raises is not None:
        h._create_and_send_otp = AsyncMock(side_effect=otp_raises)  # type: ignore[method-assign]
    else:
        h._create_and_send_otp = AsyncMock(return_value=otp_status)  # type: ignore[method-assign]
    return h


def _run(h: LoginHandler) -> None:
    """Запустить LoginHandler.post() с замоканным RateLimitRepository (разрешает)."""
    rate_repo_mock = MagicMock()
    rate_repo_mock.check_and_record = AsyncMock(return_value=True)
    with patch("app.handlers.login.RateLimitRepository", return_value=rate_repo_mock):
        asyncio.run(h.post())


# ---------------------------------------------------------------------------
# Rate-limit branch
# ---------------------------------------------------------------------------


def test_rate_limited_increments() -> None:
    before = _counter("scud_login_attempts_total",
                      {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.RATE_LIMITED.value})
    h = _make_handler({"phone": "x", "password": "y"})
    rate_repo_mock = MagicMock()
    rate_repo_mock.check_and_record = AsyncMock(return_value=False)
    with patch("app.handlers.login.RateLimitRepository", return_value=rate_repo_mock):
        asyncio.run(h.post())
    after = _counter("scud_login_attempts_total",
                     {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.RATE_LIMITED.value})
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# Upstream-unavailable branch (_check_credentials raises)
# ---------------------------------------------------------------------------


def test_upstream_unavailable_increments() -> None:
    before = _counter("scud_login_attempts_total",
                      {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.UPSTREAM_UNAVAILABLE.value})
    h = _make_handler(
        {"phone": "x", "password": "y"},
        check_raises=UpstreamUnavailableError("auth unavailable", upstream="user_api:8000"),
    )
    _run(h)
    after = _counter("scud_login_attempts_total",
                     {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.UPSTREAM_UNAVAILABLE.value})
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# Bad-credential branch (result is None)
# ---------------------------------------------------------------------------


def test_bad_credential_increments_when_user_none() -> None:
    before = _counter("scud_login_attempts_total",
                      {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.BAD_CREDENTIAL.value})
    h = _make_handler({"phone": "x", "password": "y"}, check_result=None)
    _run(h)
    after = _counter("scud_login_attempts_total",
                     {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.BAD_CREDENTIAL.value})
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# Success without 2FA
# ---------------------------------------------------------------------------


def test_success_no_2fa_increments() -> None:
    before = _counter("scud_login_attempts_total",
                      {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.SUCCESS_NO_2FA.value})
    h = _make_handler(
        {"phone": "x", "password": "y"},
        check_result=(1, False, "Имя", "worker", None),
    )
    _run(h)
    after = _counter("scud_login_attempts_total",
                     {"service": metrics.SERVICE_NAME, "outcome": LoginOutcome.SUCCESS_NO_2FA.value})
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# OTP issue — SENT
# ---------------------------------------------------------------------------


def test_otp_issue_sent_increments() -> None:
    before = _counter("scud_otp_total",
                      {"service": metrics.SERVICE_NAME, "stage": OtpStage.ISSUE.value,
                       "outcome": OtpOutcome.SENT.value})
    h = _make_handler(
        {"phone": "x", "password": "y"},
        check_result=(1, True, "Имя", "worker", 99),
        otp_status=OtpDeliveryStatus.SENT,
    )
    _run(h)
    after = _counter("scud_otp_total",
                     {"service": metrics.SERVICE_NAME, "stage": OtpStage.ISSUE.value,
                      "outcome": OtpOutcome.SENT.value})
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# OTP issue — QUEUED (SMS-primary: ни один канал не доставил → delivery unavailable)
# ---------------------------------------------------------------------------


def test_otp_issue_queued_increments_delivery_unavailable() -> None:
    before = _counter("scud_otp_total",
                      {"service": metrics.SERVICE_NAME, "stage": OtpStage.ISSUE.value,
                       "outcome": OtpOutcome.DELIVERY_UNAVAILABLE.value})
    h = _make_handler(
        {"phone": "x", "password": "y"},
        check_result=(1, True, "Имя", "worker", None),
        otp_status=OtpDeliveryStatus.QUEUED_UNTIL_BINDING,
    )
    _run(h)
    after = _counter("scud_otp_total",
                     {"service": metrics.SERVICE_NAME, "stage": OtpStage.ISSUE.value,
                      "outcome": OtpOutcome.DELIVERY_UNAVAILABLE.value})
    assert after - before == 1.0
    h.write_service_unavailable.assert_called_once()
    h.write_problem.assert_not_called()


# ---------------------------------------------------------------------------
# OTP issue — delivery unavailable (_create_and_send_otp raises)
# ---------------------------------------------------------------------------


def test_otp_delivery_unavailable_increments() -> None:
    before = _counter("scud_otp_total",
                      {"service": metrics.SERVICE_NAME, "stage": OtpStage.ISSUE.value,
                       "outcome": OtpOutcome.DELIVERY_UNAVAILABLE.value})
    h = _make_handler(
        {"phone": "x", "password": "y"},
        check_result=(1, True, "Имя", "worker", 99),
        otp_raises=UpstreamUnavailableError("notification unavailable", upstream="notification_api:8005"),
    )
    _run(h)
    after = _counter("scud_otp_total",
                     {"service": metrics.SERVICE_NAME, "stage": OtpStage.ISSUE.value,
                      "outcome": OtpOutcome.DELIVERY_UNAVAILABLE.value})
    assert after - before == 1.0


# ---------------------------------------------------------------------------
# Security invariant: no secrets in label values
# ---------------------------------------------------------------------------


def test_no_secret_in_label_sample_values() -> None:
    """Проверяет, что label-значения Prometheus счётчиков не содержат секретных данных.

    Мок-данные содержат имитацию телефона, пароля, имени, OTP-кода и токена.
    Ни один из них не должен попасть в label метрики.
    """
    secret_phone = "+79991234567"
    secret_password = "s3cr3t!"
    secret_otp = "123456"
    secret_token = "tok_very_secret"
    secret_name = "ИванИванович"

    # Запускаем ветку success_2fa (OTP sent) с «чувствительными» данными в моках
    h = _make_handler(
        {"phone": secret_phone, "password": secret_password},
        check_result=(42, True, secret_name, "worker", 99),
        otp_status=OtpDeliveryStatus.SENT,
    )
    h._issue_access_token = AsyncMock(return_value=secret_token)  # type: ignore[method-assign]
    _run(h)

    # Сканируем все sample'ы REGISTRY на предмет утечки секретов в label-значениях
    secrets = {secret_phone, secret_password, secret_otp, secret_token, secret_name}
    for metric in REGISTRY.collect():
        for sample in metric.samples:
            for label_val in sample.labels.values():
                assert label_val not in secrets, (
                    f"Secret leaked into metric label: {label_val!r} "
                    f"in {metric.name} {sample.labels}"
                )
