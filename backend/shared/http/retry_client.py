"""
HTTP-клиент с retry для inter-service вызовов.

Обёртка вокруг `tornado.httpclient.AsyncHTTPClient`:
- единый таймаут на соединение и запрос;
- ограниченное число повторов на сетевые ошибки и 5xx;
- exponential backoff между попытками;
- 4xx-ответы НЕ ретраятся (это ошибка вызова, не временный сбой).

Ретрай по методу (COR-11): таймаут/5xx — НЕОДНОЗНАЧНЫЙ сбой (upstream мог уже
обработать запрос, но ACK потерян). Для идемпотентных методов
(GET/HEAD/PUT/DELETE) повтор безопасен. Для POST повтор таймаута/5xx по
умолчанию ЗАПРЕЩЁН — иначе дубликат OTP/SMS/security-event/notification.
POST ретраится на неоднозначный сбой только при явном opt-in idempotent=True
(endpoint защищён event_uid / ON CONFLICT). Connection-refused — заведомо НЕ
доставлено (запрос не дошёл до upstream), повтор безопасен для любого метода,
включая POST.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from urllib.parse import urlsplit

from tornado.httpclient import (
    AsyncHTTPClient,
    HTTPError,
    HTTPRequest,
    HTTPResponse,
)

from ..enums import InterserviceOutcome, InterserviceStatusClass
from ..observability import get_request_id

logger = logging.getLogger(__name__)

# Prometheus-телеметрия межсервисных вызовов. Защищена try/except на случай,
# если python3-prometheus-client не установлен (зеркалит record_db_metric).

def _record_interservice_duration(
    target: str, method: str, status_class: InterserviceStatusClass, seconds: float
) -> None:
    pass


try:
    from ..metrics import (
        SERVICE_NAME as _METRICS_SERVICE_NAME,
        interservice_requests_total,
        record_interservice_duration as _record_interservice_duration,
    )
    _METRICS_AVAILABLE = True
except Exception:
    _METRICS_AVAILABLE = False


def _extract_target(url: str) -> str:
    """Хост из URL для label `target` (Docker-имя сервиса, низкая кардинальность).

    Никогда не кидает: при разборе мусора возвращает 'unknown'.
    """
    try:
        return urlsplit(url).hostname or "unknown"
    except Exception:
        return "unknown"


def _redact_url(url: str) -> str:
    """Log-safe URL: схема+хост[:порт]+путь, без query/fragment (там креды/OTP).

    Никогда не кидает: при разборе мусора возвращает '<redacted-url>'.
    """
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}{parts.path}"
    except Exception:
        return "<redacted-url>"


# Разрешённые значения label `method` для гистограммы. Нестандартные методы
# складываются в "other", чтобы cardinality оставалась ограниченной.
_METHOD_LABEL = frozenset({"GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"})


def _method_label(m: str) -> str:
    """Привести HTTP-метод к label-значению с ограниченной cardinality."""
    u = m.upper()
    return u if u in _METHOD_LABEL else "OTHER"


def _status_class(exc: BaseException | None) -> InterserviceStatusClass:
    """Преобразует итоговое исключение fetch() в status_class-label.

    None  → 2xx (успех); HTTPError 4xx → 4xx; HTTPError >=500 и ≠599 →
    5xx; всё остальное (timeout, OSError, 599) → network_error.
    """
    if exc is None:
        return InterserviceStatusClass.TWO_XX
    code = getattr(exc, "code", None)
    if isinstance(code, int) and 400 <= code < 500:
        return InterserviceStatusClass.FOUR_XX
    if isinstance(code, int) and code >= 500 and code != 599:
        return InterserviceStatusClass.FIVE_XX
    return InterserviceStatusClass.NETWORK_ERROR


def record_interservice_metric(target: str, method: str, outcome: str) -> None:
    """Инкремент scud_interservice_requests_total. Никогда не кидает —
    production-flow не должен падать из-за метрик. No-op без prometheus_client.
    """
    if not _METRICS_AVAILABLE:
        return
    try:
        interservice_requests_total.labels(
            service=_METRICS_SERVICE_NAME,
            target=target,
            method=method.upper(),
            outcome=outcome,
        ).inc()
    except Exception:
        logger.debug("Failed to record interservice metric", exc_info=True)


DEFAULT_CONNECT_TIMEOUT = 2.0
DEFAULT_REQUEST_TIMEOUT = 5.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE = 0.2  # секунды

# Идемпотентные по HTTP-семантике методы: повтор не создаёт побочного
# дубликата, поэтому неоднозначный сбой (timeout/5xx) ретраить безопасно.
IDEMPOTENT_METHODS = frozenset({"GET", "HEAD", "PUT", "DELETE", "OPTIONS"})

REQUEST_ID_HEADER = "X-Request-ID"


class RetryHTTPClient:
    """Async HTTP-клиент с retry для inter-service вызовов."""

    def __init__(
        self,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        redact_url: bool = False,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._connect_timeout = connect_timeout
        self._request_timeout = request_timeout
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base
        self._redact_url = redact_url

    async def fetch(
        self,
        url: str,
        method: str = "GET",
        body: bytes | str | None = None,
        headers: dict[str, str] | None = None,
        allow_nonstandard_methods: bool = False,
        *,
        idempotent: bool = False,
    ) -> HTTPResponse:
        """Выполняет HTTP-запрос с автоматическим retry.

        Возвращает успешный `HTTPResponse` или прокидывает последнее исключение.

        Ретрай неоднозначного сбоя (timeout/5xx) разрешён для идемпотентных
        методов (GET/HEAD/PUT/DELETE/OPTIONS). Для POST — только при
        ``idempotent=True`` (endpoint защищён от дубликатов: event_uid /
        ON CONFLICT). Connection-refused ретраится всегда (запрос точно не
        дошёл). 4xx не ретраится никогда. См. COR-11.
        """
        client = AsyncHTTPClient()
        last_exc: BaseException | None = None
        retry_ambiguous = method.upper() in IDEMPOTENT_METHODS or idempotent
        target = _extract_target(url)
        log_url = _redact_url(url) if self._redact_url else url

        # Авто-корреляция: X-Request-ID из ContextVar текущего запроса.
        # Явный заголовок вызывающего имеет приоритет (не перетираем).
        out_headers = dict(headers or {})
        rid = get_request_id()
        if rid and REQUEST_ID_HEADER not in out_headers:
            out_headers[REQUEST_ID_HEADER] = rid

        start = time.monotonic()
        for attempt in range(1, self._max_attempts + 1):
            req = HTTPRequest(
                url=url,
                method=method,
                body=body,
                headers=out_headers,
                connect_timeout=self._connect_timeout,
                request_timeout=self._request_timeout,
                allow_nonstandard_methods=allow_nonstandard_methods,
            )
            try:
                response = await client.fetch(req, raise_error=True)
                record_interservice_metric(target, method, InterserviceOutcome.SUCCESS.value)
                _record_interservice_duration(target, _method_label(method), _status_class(None), time.monotonic() - start)
                return response
            except HTTPError as exc:
                # 4xx — ошибка вызова, ретраить бессмысленно.
                if exc.code is not None and 400 <= exc.code < 500:
                    record_interservice_metric(target, method, InterserviceOutcome.ERROR.value)
                    _record_interservice_duration(target, _method_label(method), _status_class(exc), time.monotonic() - start)
                    raise
                # 5xx и 599 (timeout/connect failure) — НЕОДНОЗНАЧНЫЙ сбой.
                # Для не-идемпотентного метода без opt-in повтор может
                # продублировать уже обработанный запрос → не ретраим (COR-11).
                if not retry_ambiguous:
                    logger.warning(
                        "HTTP %s %s ambiguous failure, not retried "
                        "(non-idempotent, no opt-in): %s",
                        method, log_url, exc,
                    )
                    record_interservice_metric(target, method, InterserviceOutcome.ERROR.value)
                    _record_interservice_duration(target, _method_label(method), _status_class(exc), time.monotonic() - start)
                    raise
                last_exc = exc
                logger.warning(
                    "HTTP %s %s failed (attempt %d/%d): %s",
                    method, log_url, attempt, self._max_attempts, exc,
                )
            except (TimeoutError, OSError) as exc:
                # connection-refused — запрос ТОЧНО не дошёл до upstream
                # (соединение отвергнуто на этапе connect), повтор безопасен
                # даже для POST. Прочие OSError/timeout — неоднозначны:
                # ретраим только если метод идемпотентен или есть opt-in.
                if not (isinstance(exc, ConnectionRefusedError) or retry_ambiguous):
                    logger.warning(
                        "HTTP %s %s ambiguous connection error, not retried "
                        "(non-idempotent, no opt-in): %s",
                        method, log_url, exc,
                    )
                    record_interservice_metric(target, method, InterserviceOutcome.ERROR.value)
                    _record_interservice_duration(target, _method_label(method), _status_class(exc), time.monotonic() - start)
                    raise
                last_exc = exc
                logger.warning(
                    "HTTP %s %s connection error (attempt %d/%d): %s",
                    method, log_url, attempt, self._max_attempts, exc,
                )

            if attempt < self._max_attempts:
                record_interservice_metric(target, method, InterserviceOutcome.RETRY.value)
                base_delay = self._backoff_base * (2 ** (attempt - 1))
                jittered = base_delay * random.uniform(0.8, 1.2)
                await asyncio.sleep(jittered)

        record_interservice_metric(target, method, InterserviceOutcome.EXHAUSTED.value)
        assert last_exc is not None
        _record_interservice_duration(target, _method_label(method), _status_class(last_exc), time.monotonic() - start)
        raise last_exc
