"""REST-клиент Max Bot API (`https://platform-api.max.ru`).

Использует общий `shared.http.RetryHTTPClient` (3 ретрая + exp backoff,
4xx НЕ ретраит).

Авторизация — заголовок `Authorization: <token>` (БЕЗ `Bearer`, см. дока
Max Bot API). Токен бота выдаётся через `@MasterBot`.

Поведение по ошибкам:

- 200 → возврат полезной нагрузки.
- 400/404 → `MaxRecipientUnreachableError` (получатель не привязан, не
  начинал диалог с ботом, либо payload невалиден). Caller'у нужно НЕ
  ретраить и пометить outbox-запись `failed_at`.
- остальные 4xx, 5xx, сеть/таймауты после ретраев → `UpstreamUnavailableError`
  (Max API лежит). Caller отдаёт 503 наверх.

OTP-код НИКОГДА не логируется — в логах только `max_user_id`, статус и
длина текста.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from typing import Any

from dacite import from_dict
from dacite.exceptions import DaciteError
from tornado.httpclient import HTTPError

from shared.enums import NotificationChannel
from shared.http import RetryHTTPClient, UpstreamUnavailableError

from .transport import RecipientUnreachableError

logger = logging.getLogger(__name__)


_UPSTREAM = "max_bot_api"
_SEND_MAX_TEXT_BYTES = 4000


class MaxRecipientUnreachableError(RecipientUnreachableError):
    """Получатель в Max невалиден / не начинал диалога с ботом.

    Не путать с `UpstreamUnavailableError` — здесь сам Max API ответил
    4xx, что значит «получатель проблемный», ретраить бесполезно.
    """

    def __init__(self, message: str, *, max_user_id: int, status: int) -> None:
        super().__init__(message, address=str(max_user_id))
        self.max_user_id = max_user_id
        self.status = status


@dataclass
class MaxUser:
    """Автор события. В Max Bot API числовой id приходит во вложенном объекте."""

    user_id: int | None = None


@dataclass
class MaxUpdate:
    """Одно событие из `GET /updates`.

    Реальная схема Max Bot API: для `bot_started` `payload` — это строка
    start-параметра deep-link (наш invite-токен), а числовой id автора лежит
    в `user.user_id`. Все поля optional — Max шлёт много типов событий с
    разным набором полей; парсим лениво и пропускаем неподходящие
    (dacite игнорирует лишние ключи).
    """

    update_type: str = ""
    timestamp: int = 0
    user: MaxUser | None = None
    user_id: int | None = None
    payload: str | None = None

    @property
    def start_payload(self) -> str | None:
        """start-параметр deep-link для bot_started (наш invite-токен)."""
        return self.payload

    @property
    def max_user_id(self) -> int | None:
        """Числовой Max user_id: из вложенного `user`, иначе плоское поле."""
        if self.user is not None and self.user.user_id is not None:
            return self.user.user_id
        return self.user_id


@dataclass
class MaxUpdates:
    """Ответ `GET /updates`. `marker` — числовой курсор пагинации."""

    updates: list[MaxUpdate] = field(default_factory=list)
    marker: int | None = None


class MaxBotClient:
    """Тонкий REST-клиент Max Bot API поверх RetryHTTPClient."""

    channel = NotificationChannel.MAX

    def __init__(
        self,
        http: RetryHTTPClient,
        base_url: str,
        token: str,
        *,
        send_http: RetryHTTPClient | None = None,
    ) -> None:
        if not token:
            raise ValueError("MaxBotClient token must be non-empty")
        self._http = http
        self._send_http = send_http or http
        self._base = base_url.rstrip("/")
        self._token = token

    async def send_text(self, address: str, text: str) -> None:
        """Послать текст пользователю Max по числовому user_id.

        Raises:
            MaxRecipientUnreachableError: 400/404 от Max API.
            UpstreamUnavailableError: 5xx, сеть, таймаут.
            ValueError: текст пустой или длиннее 4000 символов.
        """
        max_user_id = int(address)
        if not text:
            raise ValueError("send_text: text must be non-empty")
        if len(text) > _SEND_MAX_TEXT_BYTES:
            raise ValueError(
                f"send_text: text length {len(text)} > {_SEND_MAX_TEXT_BYTES}",
            )

        url = f"{self._base}/messages?user_id={max_user_id}"
        body = json.dumps({"text": text, "format": "markdown", "notify": True})
        headers = {
            "Authorization": self._token,
            "Content-Type": "application/json",
        }

        try:
            resp = await self._send_http.fetch(url, method="POST", body=body, headers=headers)
        except HTTPError as exc:
            if exc.code in (400, 404):
                logger.warning(
                    "Max recipient unreachable: max_user_id=%s status=%s",
                    max_user_id, exc.code,
                )
                raise MaxRecipientUnreachableError(
                    f"max returned {exc.code} for user_id={max_user_id}",
                    max_user_id=max_user_id,
                    status=exc.code,
                ) from exc
            logger.error("Max send returned %s for max_user_id=%s", exc.code, max_user_id)
            raise UpstreamUnavailableError(
                f"max returned {exc.code}",
                upstream=_UPSTREAM,
                status=exc.code,
            ) from exc
        except Exception as exc:
            logger.error("Max send failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc

        if resp.code != 200:
            raise UpstreamUnavailableError(
                f"max returned {resp.code}",
                upstream=_UPSTREAM,
                status=resp.code,
            )
        logger.info(
            "Max message sent: max_user_id=%s status=%s text_len=%d",
            max_user_id, resp.code, len(text),
        )

    async def poll_updates(
        self, marker: int | None = None, long_poll_timeout_s: int = 30,
    ) -> MaxUpdates:
        """Обёртка `GET /updates?marker=...&timeout=...` для long-polling.

        Возвращает dataclass с `updates: list[MaxUpdate]` и следующим
        `marker`-курсором. Long-polling: Max API подвешивает соединение
        до `long_poll_timeout_s` секунд если updates'ов нет. Это серверный
        long-poll-дедлайн Max API (query-параметр `timeout`), а не
        client-side asyncio-таймаут — обёртывать вызов в `asyncio.timeout`
        не требуется.
        """
        url = f"{self._base}/updates?timeout={long_poll_timeout_s}"
        if marker is not None:
            url += f"&marker={marker}"
        headers = {"Authorization": self._token}

        try:
            # request_timeout у клиента 5s — для long-polling недостаточно;
            # tornado без перенастройки бросит TimeoutError. Поэтому ставим
            # явно увеличенный таймаут на запрос через инстанс клиента.
            resp = await self._http.fetch(url, method="GET", headers=headers)
        except HTTPError as exc:
            logger.error("Max poll_updates returned %s", exc.code)
            raise UpstreamUnavailableError(
                f"max returned {exc.code}",
                upstream=_UPSTREAM,
                status=exc.code,
            ) from exc
        except Exception as exc:
            logger.warning("Max poll_updates failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc

        try:
            parsed: Any = json.loads(resp.body)
        except (ValueError, TypeError) as exc:
            raise UpstreamUnavailableError(
                f"max /updates returned non-JSON body: {exc}",
                upstream=_UPSTREAM,
            ) from exc

        if not isinstance(parsed, dict):
            raise UpstreamUnavailableError(
                f"max /updates returned non-dict body: {type(parsed).__name__}",
                upstream=_UPSTREAM,
            )

        # Парсим апдейты по одному: Max шлёт разнотипные события, и одно
        # событие с неожиданной формой не должно ронять весь батч (и поллер).
        raw_updates = parsed.get("updates")
        updates: list[MaxUpdate] = []
        if isinstance(raw_updates, list):
            for raw in raw_updates:
                if not isinstance(raw, dict):
                    continue
                try:
                    updates.append(from_dict(MaxUpdate, raw))
                except DaciteError as exc:
                    logger.warning(
                        "Skipping unparseable Max update (type=%s): %s",
                        raw.get("update_type"), exc,
                    )
        return MaxUpdates(updates=updates, marker=parsed.get("marker"))
