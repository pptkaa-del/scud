from app.main import (
    DEFAULT_STEP_UP_ISSUE_TIMEOUT_SECONDS,
    STEP_UP_ISSUE_TIMEOUT_ENV,
    build_step_up_http,
)


def test_default_timeout_is_50(monkeypatch):
    monkeypatch.delenv(STEP_UP_ISSUE_TIMEOUT_ENV, raising=False)
    assert DEFAULT_STEP_UP_ISSUE_TIMEOUT_SECONDS == 50.0
    assert build_step_up_http()._request_timeout == 50.0


def test_timeout_from_env(monkeypatch):
    monkeypatch.setenv(STEP_UP_ISSUE_TIMEOUT_ENV, "70")
    assert build_step_up_http()._request_timeout == 70.0
