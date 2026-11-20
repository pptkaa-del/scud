import asyncio
import contextlib
import logging

from shared.http import RetryHTTPClient

_SECRET_URL = (
    "http://127.0.0.1:1/cgi/WebCGI"
    "?1500101=account=u&password=SUPERSECRET123&content=OTP654321"
)


def test_redact_url_true_hides_secrets_in_warning_log(caplog):
    caplog.set_level(logging.WARNING)
    client = RetryHTTPClient(
        connect_timeout=1.0, request_timeout=1.0, max_attempts=1, redact_url=True,
    )
    with contextlib.suppress(Exception):
        asyncio.run(client.fetch(_SECRET_URL, method="GET"))

    text = caplog.text
    assert "SUPERSECRET123" not in text
    assert "OTP654321" not in text
    assert "127.0.0.1" in text


def test_redact_url_default_false_is_backward_compatible(caplog):
    caplog.set_level(logging.WARNING)
    client = RetryHTTPClient(
        connect_timeout=1.0, request_timeout=1.0, max_attempts=1,
    )
    with contextlib.suppress(Exception):
        asyncio.run(client.fetch(_SECRET_URL, method="GET"))

    assert "SUPERSECRET123" in caplog.text
