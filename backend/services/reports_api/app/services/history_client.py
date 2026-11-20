"""HTTP-клиент для чтения command-history из history_api.

Все вызовы идут через `RetryHTTPClient` (3 попытки + exp backoff).
При исчерпании retry на 5xx/network или неожиданном статусе → бросается
`UpstreamUnavailableError(upstream="history_api")`; вызывающий handler/worker
должен интерпретировать это как деградацию инфры (503), а не пустой результат.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
from urllib.parse import urlencode

from dacite import Config, from_dict
from tornado.httpclient import HTTPError

from shared.http import RetryHTTPClient
from shared.http.errors import UpstreamUnavailableError

logger = logging.getLogger(__name__)

_UPSTREAM = "history_api"
_COMMANDS_HISTORY_PATH = "/internal/api/v1/commands/history"
_COMMAND_TYPE_OPEN = "open"
_COMMAND_STATUS_EXECUTED = "executed"
_ORDER_EXECUTED_ASC = "executed_at_asc"


@dataclass(slots=True)
class OpenEvent:
    created_by: int | None
    locker_inventory_number: str | None
    user_full_name: str | None
    executed_at: datetime


_CONFIG = Config(type_hooks={datetime: datetime.fromisoformat})


class HistoryReportClient:
    """Async HTTP-клиент к history_api для нужд reports_api."""

    def __init__(self, base_url: str, http: RetryHTTPClient | None = None) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()

    async def fetch_open_events(
        self,
        date_from: datetime,
        date_to: datetime,
        limit: int,
        offset: int,
    ) -> list[OpenEvent]:
        """GET /internal/api/v1/commands/history (open+executed в окне).

        Использует обобщённый эндпойнт command-history с фильтрами
        `command_type=open`, `status=executed` и сортировкой по `executed_at`.
        Полные строки command_history парсятся в `OpenEvent`; лишние ключи
        dacite игнорирует (strict не задан в `_CONFIG`).

        Returns:
            Список `OpenEvent` из поля `items` ответа (одна страница).

        Raises:
            UpstreamUnavailableError(upstream=history_api) при ошибке upstream.
        """
        qs = urlencode(
            {
                "command_type": _COMMAND_TYPE_OPEN,
                "status": _COMMAND_STATUS_EXECUTED,
                "executed_from": date_from.isoformat(),
                "executed_to": date_to.isoformat(),
                "order": _ORDER_EXECUTED_ASC,
                "limit": limit,
                "offset": offset,
            }
        )
        url = f"{self._base}{_COMMANDS_HISTORY_PATH}?{qs}"
        try:
            resp = await self._http.fetch(url, method="GET")
        except HTTPError as exc:
            logger.error("history_api commands/history returned %s: %s", exc.code, exc)
            raise UpstreamUnavailableError(
                f"history_api returned {exc.code}", upstream=_UPSTREAM, status=exc.code
            ) from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            logger.error("history_api commands/history failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc
        body = json.loads(resp.body)
        return [from_dict(OpenEvent, item, config=_CONFIG) for item in body["items"]]
