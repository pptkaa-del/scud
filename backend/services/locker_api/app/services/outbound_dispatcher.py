"""Фоновый воркер доставки исходящих событий в history_api.

Алгоритм каждой итерации:
  1. SELECT недоставленных событий с `next_attempt_at <= now()` (`fetch_pending`).
  2. Для каждого — POST на `endpoint` (хост из env `HISTORY_API_URL`).
     - Для канала `media` payload содержит `file_bytes_b64` (base64-строка),
       `form_fields` и `file_name`/`file_mime`; dispatcher собирает multipart.
     - Прочие каналы — JSON-тело `payload`.
  3. При успехе — `mark_delivered`. При ошибке — `mark_failed` с экспоненциальной
     задержкой следующей попытки; после `max_attempts` — dead-letter.
  4. Каждые `RETENTION_INTERVAL_SECONDS` чистит доставленные старше
     `OUTBOUND_RETENTION_DAYS`.

Гарантии:
  - At-least-once: HTTP-ретрай, плюс запись в очереди живёт до успешной
    доставки (никаких потерь при кратком падении history_api).
  - Дедупликации в самой очереди НЕТ. Идемпотентность обеспечивается ниже по
    потоку: history_api дедуплицирует по `event_uid` через
    `ON CONFLICT DO NOTHING`, поэтому повторная доставка одного события
    (ретрай, истёкший lease, две реплики) безвредна.
  - `fetch_pending` теперь заявляет строки атомарно (`FOR UPDATE SKIP LOCKED`
    + lease на `next_attempt_at`), так что конкурентные реплики dispatcher'а
    не хватают одни и те же строки — это best-effort multi-replica safety для
    пути outbox → history_api. ВНИМАНИЕ: путь доставки WS-команд на устройства
    остаётся single-replica (CommandQueue держит транспорты in-process;
    см. DEPLOYMENT.md, раздел про масштабирование).
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import logging
import os
import time
from typing import Any
import uuid

from shared.database import AsyncPostgres
from shared.http import RetryHTTPClient
from shared.metrics import record_outbound_dead_lettered
from shared.utils import body_sha256, compute_request_signature

from ..locker_data.db.repositories.outbound import OutboundEvent, OutboundRepository

logger = logging.getLogger(__name__)


CHANNEL_MEDIA = "media"

# HTTP-метод доставки. Канон HMAC-подписи привязан к method+path (SEC-20):
# path берётся из event.endpoint и совпадает с self.request.path на стороне
# history_api InternalHmacMixin.
_METHOD = "POST"


def _exp_backoff_seconds(attempt: int, base: float = 2.0, cap: float = 600.0) -> float:
    """Экспоненциальный backoff: base * 2^(attempt-1), но не больше cap (10 минут)."""
    delay: float = base * (2 ** max(0, attempt - 1))
    return min(delay, cap)


class OutboundDispatcher:
    """Воркер доставки событий из `outbound_history_events` в history_api."""

    def __init__(
        self,
        db: AsyncPostgres,
        history_api_url: str | None = None,
        poll_interval_seconds: float = 2.0,
        batch_size: int = 100,
        retention_days: int = 7,
        retention_interval_seconds: float = 3600.0,
        hmac_key: bytes | None = None,
        max_batch_bytes: int | None = None,
        concurrency: int = 8,
        max_attempts: int = 20,
    ) -> None:
        self._db = db
        self._repo = OutboundRepository(db)
        self._http = RetryHTTPClient()
        # Бюджет батча по суммарному размеру payload'ов (см.
        # OutboundRepository.fetch_pending): не даёт батчу из крупных media-фото
        # загрузить сотни МБ разом (тот самый OOM). None — без бюджета.
        self._max_batch_bytes = max_batch_bytes
        # Сколько доставок держать в полёте одновременно. Последовательная
        # доставка (1 в полёте) не успевала разгребать бэклог; семафор ограничивает
        # число одновременных соединений к history_api. Явный <= 0 — это
        # мисконфиг env OUTBOUND_DISPATCHER_CONCURRENCY: фейлимся громко, а не
        # тихо создаём Semaphore(0), который навсегда заклинит доставку.
        if concurrency <= 0:
            raise ValueError("concurrency must be > 0")
        self._concurrency = concurrency
        # После скольких неуспешных попыток парковать событие в dead-letter
        # вместо бесконечного ретрая (poison-событие / затяжная недоступность
        # history_api). Должен пережидать реалистичный outage: при cap-backoff
        # 600s ~20 попыток ≈ несколько часов до парковки.
        self._max_attempts = max(1, max_attempts)
        self._history_api_url = history_api_url or os.getenv(
            "HISTORY_API_URL", "http://history_api:8002",
        )
        # SEC-05: общий с history_api HMAC-ключ. Подписывается raw-body
        # каждого POST на internal-эндпойнты (SEC-20: канон привязан к
        # методу и пути — timestamp + METHOD + path + sha256(body)).
        # None → запросы летят без подписи (dev/CI без секрета; history_api
        # в таком же режиме пропускает).
        self._hmac_key = hmac_key
        self._poll_interval = poll_interval_seconds
        self._batch_size = batch_size
        self._retention = timedelta(days=retention_days)
        self._retention_interval = retention_interval_seconds
        # Event-based shutdown: позволяет проверять флаг mid-batch без того,
        # чтобы mypy сужал bool до Literal[True] после `while self._running:`.
        # При деградации history_api batch на 100 событий с cap-backoff 600s
        # без mid-batch break мог затянуть graceful shutdown на десятки минут.
        self._stop_event = asyncio.Event()
        self._last_retention_at = 0.0

    async def start(self) -> None:
        """Запустить цикл доставки. Останавливается через `stop()`."""
        self._stop_event.clear()
        loop = asyncio.get_running_loop()
        logger.info(
            "OutboundDispatcher started (target=%s, batch=%d, poll=%.1fs, retention=%dd)",
            self._history_api_url, self._batch_size, self._poll_interval, self._retention.days,
        )

        while not self._stop_event.is_set():
            events: list[OutboundEvent] = []
            try:
                events = await self._repo.fetch_pending(
                    self._batch_size, max_bytes=self._max_batch_bytes,
                )
                if events:
                    await self._deliver_batch(events)

                now = loop.time()
                if now - self._last_retention_at >= self._retention_interval:
                    self._last_retention_at = now
                    try:
                        deleted = await self._repo.cleanup_delivered(self._retention)
                        if deleted:
                            logger.info("OutboundDispatcher: retention deleted %d delivered events", deleted)
                    except Exception as exc:
                        logger.error("OutboundDispatcher retention failed: %s", exc, exc_info=True)
            except Exception as exc:
                logger.error("OutboundDispatcher iteration failed: %s", exc, exc_info=True)

            # Drain-fast: only idle-sleep when the last batch was EMPTY. A full
            # backlog is drained back-to-back (the unconditional 2s sleep after
            # every batch was the telemetry-delivery throughput cap).
            if not events:
                await asyncio.sleep(self._poll_interval)

        logger.info("OutboundDispatcher stopped")

    def stop(self) -> None:
        self._stop_event.set()

    async def _deliver_batch(self, events: list[OutboundEvent]) -> None:
        """Доставить батч с ограниченной конкуренцией (`self._concurrency` в полёте).

        Конкурентная доставка повышает throughput: последовательная (1 в полёте)
        не успевала разгребать накопившийся бэклог — при ~0.5 c на POST это ~2
        события/с, и десятки тысяч строк затягивались на часы. Семафор не даёт
        открыть слишком много соединений к history_api разом. Порядок доставки
        ВНУТРИ батча не гарантируется: history_api идемпотентен по `event_uid`,
        а телеметрия попорядочна на уровне отдельного reading'а, поэтому FIFO
        внутри батча не требуется (межбатчевый FIFO даёт `fetch_pending ORDER BY
        id`). `stop_event` проверяется на входе каждого слота — graceful-shutdown
        не ждёт весь батч. Исключения отдельных доставок поглощаются внутри
        `_deliver` (mark_failed/dead-letter), поэтому gather без
        return_exceptions: ни одна доставка наружу не кидает.
        """
        sem = asyncio.Semaphore(self._concurrency)

        async def _one(event: OutboundEvent) -> None:
            async with sem:
                if self._stop_event.is_set():
                    return
                await self._deliver(event)

        await asyncio.gather(*(_one(event) for event in events))

    async def _deliver(self, event: OutboundEvent) -> None:
        try:
            url = f"{self._history_api_url}{event.endpoint}"
            if event.channel == CHANNEL_MEDIA:
                # _build_multipart декодирует base64 из памяти — но делаем
                # это в thread executor для однородности с будущими большими payload.
                body, headers = await asyncio.to_thread(self._build_multipart, event.payload)
            else:
                body = json.dumps(event.payload).encode("utf-8")
                headers = {"Content-Type": "application/json"}
            self._sign(event.endpoint, body, headers)
            await self._http.fetch(url, method=_METHOD, body=body, headers=headers)
            await self._repo.mark_delivered(event.id)
            logger.debug(
                "Delivered outbound event #%d (%s → %s)",
                event.id, event.channel, event.endpoint,
            )
        except Exception as exc:
            next_attempt = event.attempt_count + 1
            # Dead-letter: исчерпали бюджет попыток — перестаём ретраить, паркуем
            # событие (хвост очереди не растёт безгранично из-за poison-события
            # или затяжной недоступности history_api), сигналим метрикой + ERROR.
            if next_attempt >= self._max_attempts:
                try:
                    await self._repo.mark_dead_lettered(event.id, repr(exc))
                except Exception as mark_exc:
                    logger.error(
                        "OutboundDispatcher: mark_dead_lettered crashed for event "
                        "#%d (%s): %s",
                        event.id, event.channel, mark_exc, exc_info=True,
                    )
                else:
                    record_outbound_dead_lettered(event.channel)
                    logger.error(
                        "OutboundDispatcher: event #%d (%s) DEAD-LETTERED after %d "
                        "attempts — parked for manual review: %s",
                        event.id, event.channel, next_attempt, exc,
                    )
                return
            delay = _exp_backoff_seconds(next_attempt)
            try:
                await self._repo.mark_failed(event.id, repr(exc), delay)
            except Exception as mark_exc:
                # mark_failed сам упал — backoff не записался, событие
                # потенциально hot-loop-нётся следующей итерацией. Ждём
                # делей вручную чтобы не выжигать БД и логи.
                logger.error(
                    "OutboundDispatcher: mark_failed crashed for event #%d (%s): %s; "
                    "manual backoff %.1fs before next attempt",
                    event.id, event.channel, mark_exc, delay,
                    exc_info=True,
                )
                await asyncio.sleep(delay)
            logger.warning(
                "Failed to deliver outbound event #%d (%s, attempt %d): %s; retry in %.1fs",
                event.id, event.channel, next_attempt, exc, delay,
            )

    def _sign(self, path: str, body: bytes, headers: dict[str, str]) -> None:
        """Добавить X-Internal-* подпись над method+path+raw-body (SEC-05/SEC-20).

        Работает одинаково для JSON и multipart: подписывается ровно тот
        ``body``, что уходит в сеть (sha256 над сырыми байтами ДО парсинга
        form-data на стороне history_api). ``path`` — ``event.endpoint``,
        который history_api видит как ``self.request.path``. No-op если ключ
        не сконфигурирован.
        """
        if self._hmac_key is None:
            return
        timestamp = str(int(time.time()))
        signature = compute_request_signature(
            timestamp=timestamp,
            method=_METHOD,
            path=path,
            body_sha256_hex=body_sha256(body),
            key=self._hmac_key,
        )
        headers["X-Internal-Timestamp"] = timestamp
        headers["X-Internal-Signature"] = signature

    @staticmethod
    def _build_multipart(payload: dict[str, Any]) -> tuple[bytes, dict[str, str]]:
        """Собрать multipart/form-data из payload для канала `media`.

        Ожидаемый payload:
          {
            "file_bytes_b64": "<base64-encoded bytes>",
            "file_name": "media-<transfer_id>",
            "file_mime": "image/jpeg",
            "form_fields": {"locker_id": "...", "kind": "photo", ...}
          }
        """
        import base64 as _base64

        file_bytes = _base64.b64decode(payload["file_bytes_b64"])
        mime_type = payload.get("file_mime", "application/octet-stream")
        file_name = payload.get("file_name", "media")
        form_fields = payload.get("form_fields", {})

        boundary = uuid.uuid4().hex
        crlf = b"\r\n"
        parts: list[bytes] = []
        for name, value in form_fields.items():
            if value is None:
                continue
            parts.append(f"--{boundary}".encode())
            parts.append(f'Content-Disposition: form-data; name="{name}"'.encode())
            parts.append(b"")
            parts.append(str(value).encode("utf-8"))
        parts.append(f"--{boundary}".encode())
        parts.append(
            f'Content-Disposition: form-data; name="file"; filename="{file_name}"'.encode()
        )
        parts.append(f"Content-Type: {mime_type}".encode())
        parts.append(b"")
        parts.append(file_bytes)
        parts.append(f"--{boundary}--".encode())
        parts.append(b"")

        body = crlf.join(parts)
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        return body, headers
