import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from tornado.httpclient import HTTPError

from app.services.sms_gateway_client import (
    SmsGatewayClient,
    chunk_ucs2,
    mask_msisdn,
    normalize_msisdn,
)
from app.services.transport import RecipientUnreachableError
from shared.http import UpstreamUnavailableError


def test_normalize_msisdn_uses_international_plus_format():
    # Без «+» модем шлёт 7XXXXXXXXXX как национальный номер — «Ошибка».
    assert normalize_msisdn("+79001234567") == "+79001234567"
    assert normalize_msisdn("8 (900) 123-45-67") == "+79001234567"
    assert normalize_msisdn("79001234567") == "+79001234567"
    assert normalize_msisdn("+375291234567") == "+375291234567"


def test_chunk_ucs2_splits_at_70():
    text = "я" * 141
    parts = chunk_ucs2(text, limit=70)
    assert [len(p) for p in parts] == [70, 70, 1]


def _client(resp_body=b"", *, http=None):
    http = http or MagicMock()
    return SmsGatewayClient(http=http, base_url="http://gw:7777",
                            account="u", password="p", min_interval_s=0.0)


def _resp(body: bytes, code: int = 200):
    r = MagicMock()
    r.code = code
    r.body = body
    return r


def test_send_text_success_marks_ok():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(b"Success Message: Commit successfully!"))
    c = _client(http=http)
    asyncio.run(c.send_text("79001234567", "Kod: 123456"))
    # URL carries destination + urlencoded content, GET, empty body
    url = http.fetch.call_args.args[0]
    assert "1500101=account=u" in url
    assert "destination=%2B79001234567" in url
    assert http.fetch.call_args.kwargs["method"] == "GET"


def test_send_text_commit_timeout_counts_as_accepted():
    # Шлюз отвечает так, когда модем занят: SMS остаётся в очереди и уходит.
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(b"Response: Failed\nMessage: Commit Timeout!"))
    c = _client(http=http)
    asyncio.run(c.send_text("79001234567", "x"))


def test_send_text_commit_failed_is_permanent():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(b"Response: Failed\nMessage: Commit failed!"))
    c = _client(http=http)
    with pytest.raises(RecipientUnreachableError):
        asyncio.run(c.send_text("79001234567", "x"))


def test_send_text_auth_failure_is_permanent():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(b"Response: Error\nMessage: Authentication failed"))
    c = _client(http=http)
    with pytest.raises(RecipientUnreachableError):
        asyncio.run(c.send_text("79001234567", "x"))


def test_send_text_5xx_is_transient():
    http = MagicMock()
    http.fetch = AsyncMock(side_effect=HTTPError(502))
    c = _client(http=http)
    with pytest.raises(UpstreamUnavailableError):
        asyncio.run(c.send_text("79001234567", "x"))


def test_send_text_network_error_is_transient():
    http = MagicMock()
    http.fetch = AsyncMock(side_effect=ConnectionRefusedError("no tunnel"))
    c = _client(http=http)
    with pytest.raises(UpstreamUnavailableError):
        asyncio.run(c.send_text("79001234567", "x"))


def test_send_text_cyrillic_is_percent_encoded():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(b"Commit successfully"))
    c = _client(http=http)
    asyncio.run(c.send_text("79001234567", "Код 42"))
    url = http.fetch.call_args.args[0]
    assert "%D0%9A%D0%BE%D0%B4" in url  # "Код" percent-encoded UTF-8
    assert "Код" not in url


def test_send_text_multipart_sends_each_chunk():
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(b"Commit successfully"))
    c = _client(http=http)
    asyncio.run(c.send_text("79001234567", "я" * 141))
    assert http.fetch.call_count == 3  # 70 + 70 + 1


def test_send_text_empty_text_rejected():
    http = MagicMock()
    http.fetch = AsyncMock()
    c = _client(http=http)
    with pytest.raises(ValueError):
        asyncio.run(c.send_text("79001234567", ""))
    http.fetch.assert_not_called()


def test_mask_msisdn_keeps_plus_country_digit_and_last_four():
    assert mask_msisdn("+79001234567") == "+7******4567"
    assert mask_msisdn("+375291234567") == "+3*******4567"
    assert mask_msisdn("123") == "***"


@pytest.mark.parametrize(
    ("body", "exc"),
    [
        (b"Commit successfully", None),
        (b"Commit Timeout!", None),
        (b"Commit failed!", RecipientUnreachableError),
    ],
)
def test_send_text_logs_do_not_contain_full_msisdn(caplog, body, exc):
    http = MagicMock()
    http.fetch = AsyncMock(return_value=_resp(body))
    c = _client(http=http)
    with caplog.at_level("DEBUG"):
        if exc is None:
            asyncio.run(c.send_text("79001234567", "x"))
        else:
            with pytest.raises(exc):
                asyncio.run(c.send_text("79001234567", "x"))
    assert caplog.records
    assert "79001234567" not in caplog.text
    assert "+7******4567" in caplog.text or exc is not None


def test_send_text_http_error_log_masks_msisdn(caplog):
    http = MagicMock()
    http.fetch = AsyncMock(side_effect=HTTPError(502))
    c = _client(http=http)
    with caplog.at_level("DEBUG"), pytest.raises(UpstreamUnavailableError):
        asyncio.run(c.send_text("79001234567", "x"))
    assert "79001234567" not in caplog.text
    assert "+7******4567" in caplog.text
