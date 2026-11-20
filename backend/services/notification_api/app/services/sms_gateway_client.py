"""HTTP-клиент SMS-шлюза Yeastar TG (CGI-интерфейс ``1500101``).

Отправка: ``GET /cgi/WebCGI?1500101=account=<u>&password=<p>&port=<n>&
destination=<msisdn>&content=<urlencoded>`` — GET, пустое тело, креды в
query. Ответ — plain text: ``Commit successfully`` → принято шлюзом;
``Commit Timeout`` → модем занят, SMS осталась в очереди шлюза и уходит
позже, тоже «принято» (иначе fallback на Max дал бы второй код);
``Authentication failed``, ``Commit failed`` и прочее → сбой. Подтверждения
доставки на телефон нет — ``sent`` означает «принято шлюзом».

Номер — в международном формате ``+7XXXXXXXXXX`` (``+`` → ``%2B``): без
плюса модем отдаёт ``7XXXXXXXXXX`` в сеть как национальный номер и SMS
не уходит (проверено на живом TG200).

Аппаратный лимит одного GSM-порта ~1 SMS/10 c → FIFO-пейсер раздаёт слоты
отправки по порядку прихода; ожидание слота ограничено ``max_queue_wait_s``,
иначе отказ без обращения к шлюзу. Кириллица → UCS-2 (70 симв/часть) → чанкуем сами.
НИКОГДА не логировать content/URL (в URL пароль и текст).
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import logging
import re
import time
from urllib.parse import urlencode

from tornado.httpclient import HTTPError

from shared.enums import NotificationChannel
from shared.http import RetryHTTPClient, UpstreamUnavailableError

from .transport import RecipientUnreachableError

logger = logging.getLogger(__name__)

_UPSTREAM = "sms_gateway"
_CGI_SEND_ID = "1500101"
_SUCCESS_MARKER = "Commit successfully"
_QUEUED_MARKER = "Commit Timeout"
_AUTH_FAIL_MARKER = "Authentication failed"
_UCS2_PART_LIMIT = 70
_RU_MSISDN_LEN = 11
_RU_COUNTRY_PREFIX = "7"
_RU_NATIONAL_PREFIX = "8"
_MASK_HEAD_DIGITS = 1
_MASK_TAIL_DIGITS = 4
_MASK_CHAR = "*"


def normalize_msisdn(phone: str) -> str:
    """`+7XXXXXXXXXX` / `7XXXXXXXXXX` / `8XXXXXXXXXX` → `+7XXXXXXXXXX`."""
    digits = re.sub(r"\D", "", phone)
    if len(digits) == _RU_MSISDN_LEN and digits.startswith(_RU_NATIONAL_PREFIX):
        digits = _RU_COUNTRY_PREFIX + digits[1:]
    return "+" + digits


def mask_msisdn(msisdn: str) -> str:
    """`+79001234567` → `+7******4567`: для логов, полный номер не пишется."""
    digits = re.sub(r"\D", "", msisdn)
    if len(digits) <= _MASK_HEAD_DIGITS + _MASK_TAIL_DIGITS:
        return _MASK_CHAR * len(digits)
    hidden = len(digits) - _MASK_HEAD_DIGITS - _MASK_TAIL_DIGITS
    return f"+{digits[:_MASK_HEAD_DIGITS]}{_MASK_CHAR * hidden}{digits[-_MASK_TAIL_DIGITS:]}"


def chunk_ucs2(text: str, limit: int = _UCS2_PART_LIMIT) -> list[str]:
    """Разбить текст на части ≤`limit` символов (UCS-2 сегментация)."""
    if not text:
        return []
    return [text[i : i + limit] for i in range(0, len(text), limit)]


class SmsGatewayClient:
    """Транспорт SMS поверх RetryHTTPClient. channel == SMS."""

    channel = NotificationChannel.SMS

    def __init__(
        self,
        http: RetryHTTPClient,
        base_url: str,
        account: str,
        password: str,
        *,
        port: int = 1,
        min_interval_s: float = 10.0,
        max_queue_wait_s: float = 10.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not account or not password:
            raise ValueError("SmsGatewayClient requires non-empty account/password")
        if max_queue_wait_s < 0:
            raise ValueError("max_queue_wait_s must be >= 0")
        self._http = http
        self._base = base_url.rstrip("/")
        self._account = account
        self._password = password
        self._port = port
        self._min_interval_s = min_interval_s
        self._max_queue_wait_s = max_queue_wait_s
        self._clock = clock
        self._sleep = sleep
        self._next_send_at = 0.0

    async def send_text(self, address: str, text: str) -> None:
        if not text:
            raise ValueError("send_text: text must be non-empty")
        msisdn = normalize_msisdn(address)
        parts = chunk_ucs2(text)
        slots = self._reserve(len(parts), msisdn)
        for part, slot in zip(parts, slots, strict=True):
            await self._wait_until(slot)
            await self._send_one(msisdn, part)

    def _reserve(self, count: int, msisdn: str) -> list[float]:
        """Атомарно (без await) занять `count` слотов подряд.

        Слот = max(now, next_send_at): порядок слотов совпадает с порядком
        вызовов. Ожидание первой части сверх `max_queue_wait_s` → отказ без
        резервирования; остальные части уже заняты и отказать им нельзя.
        """
        if self._min_interval_s <= 0:
            return [0.0] * count
        now = self._clock()
        first = max(now, self._next_send_at)
        wait = first - now
        if wait > self._max_queue_wait_s:
            logger.warning(
                "SMS pacer queue full: destination=%s wait=%.1fs limit=%.1fs",
                mask_msisdn(msisdn), wait, self._max_queue_wait_s,
            )
            raise UpstreamUnavailableError(
                f"sms gateway queue wait {wait:.1f}s exceeds {self._max_queue_wait_s:.1f}s",
                upstream=_UPSTREAM,
            )
        self._next_send_at = first + count * self._min_interval_s
        return [first + i * self._min_interval_s for i in range(count)]

    async def _wait_until(self, slot: float) -> None:
        delay = slot - self._clock()
        if delay > 0:
            await self._sleep(delay)

    async def _send_one(self, msisdn: str, part: str) -> None:
        # 1500101=account=<u> — id CGI сам является первым query-ключом,
        # его "значение" начинает цепочку параметров. НЕ нормализовать в ?id=.
        head = f"{_CGI_SEND_ID}=account={self._account}"
        tail = urlencode(
            {"password": self._password, "port": self._port,
             "destination": msisdn, "content": part}
        )
        url = f"{self._base}/cgi/WebCGI?{head}&{tail}"

        try:
            resp = await self._http.fetch(url, method="GET")
        except HTTPError as exc:
            logger.error("SMS gateway HTTP %s for destination=%s", exc.code, mask_msisdn(msisdn))
            raise UpstreamUnavailableError(
                f"sms gateway returned {exc.code}", upstream=_UPSTREAM, status=exc.code,
            ) from exc
        except Exception as exc:
            logger.error("SMS gateway send failed: %s", type(exc).__name__)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc

        body = (resp.body or b"").decode("utf-8", "replace")
        if _SUCCESS_MARKER in body:
            logger.info("SMS accepted by gateway: destination=%s", mask_msisdn(msisdn))
            return
        if _QUEUED_MARKER in body:
            logger.warning("SMS queued by busy gateway modem: destination=%s", mask_msisdn(msisdn))
            return
        if _AUTH_FAIL_MARKER in body:
            logger.error("SMS gateway auth failed (check API creds/allowlist)")
            raise RecipientUnreachableError("sms gateway auth failed", address=msisdn)
        # Незнакомый ответ — считаем недоставленным на сторону получателя.
        logger.warning("SMS gateway unexpected response for destination=%s", mask_msisdn(msisdn))
        raise RecipientUnreachableError("sms gateway rejected message", address=msisdn)
