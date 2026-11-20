"""HTTP-клиент для резолва ФИО пользователей из user_api.

Используется генератором `locker_open`: `command_history.user_full_name`
никогда не заполняется locker_api, поэтому ФИО резолвится по `created_by`
во время генерации отчёта через внутренний эндпоинт user_api
`GET /internal/users/names?ids=1,2,3`.

Все вызовы идут через `RetryHTTPClient` (3 попытки + exp backoff). При
исчерпании retry на 5xx/network или неожиданном статусе → бросается
`UpstreamUnavailableError(upstream="user_api")`; вызывающий worker должен
интерпретировать это как деградацию инфры (job failed/retry), а не пустой
результат — поведение зеркалит `HistoryReportClient`.
"""

from __future__ import annotations

import json
import logging
import time
from urllib.parse import urlencode

from tornado.httpclient import HTTPError

from shared.http import RetryHTTPClient
from shared.http.errors import UpstreamUnavailableError
from shared.utils import body_sha256, compute_request_signature, signed_path

logger = logging.getLogger(__name__)

_UPSTREAM = "user_api"
_NAMES_PATH = "/internal/users/names"
# Подписываемый method. Селектор лежит в query (?ids=...), поэтому подпись
# снимается с signed_path(path, qs) — канонизированный query входит в канон,
# подмену ids= в пределах replay-окна отвергает user_api (SEC-10).
_NAMES_METHOD = "GET"


class UserApiClient:
    """Async HTTP-клиент к user_api для резолва ФИО по id пользователя."""

    def __init__(
        self,
        base_url: str,
        http: RetryHTTPClient | None = None,
        hmac_key: bytes | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()
        # PII-read HMAC-ключ (internal_user_read_hmac_key). None → dev/CI
        # fail-open: подпись не добавляется, user_api без ключа её не требует.
        self._hmac_key = hmac_key

    async def fetch_names(self, user_ids: list[int]) -> dict[int, str]:
        """GET /internal/users/names?ids=...

        Returns:
            Словарь `{id: full_name}`. Отсутствующие/несуществующие id
            опускаются user_api → их в словаре не будет.

        Raises:
            UpstreamUnavailableError(upstream=user_api) при ошибке upstream.
        """
        if not user_ids:
            return {}
        qs = urlencode({"ids": ",".join(str(i) for i in user_ids)})
        url = f"{self._base}{_NAMES_PATH}?{qs}"
        headers: dict[str, str] = {}
        if self._hmac_key is not None:
            timestamp = str(int(time.time()))
            headers["X-Internal-Timestamp"] = timestamp
            # signed_path сворачивает канонизированный query в подписываемый
            # path; user_api проверяет тем же signed_path → подписи сходятся.
            headers["X-Internal-Signature"] = compute_request_signature(
                timestamp=timestamp,
                method=_NAMES_METHOD,
                path=signed_path(_NAMES_PATH, qs),
                body_sha256_hex=body_sha256(b""),
                key=self._hmac_key,
            )
        try:
            resp = await self._http.fetch(url, method=_NAMES_METHOD, headers=headers)
        except HTTPError as exc:
            logger.error("user_api names returned %s: %s", exc.code, exc)
            raise UpstreamUnavailableError(
                f"user_api returned {exc.code}", upstream=_UPSTREAM, status=exc.code
            ) from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            logger.error("user_api names failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc
        body = json.loads(resp.body)
        return {int(k): v for k, v in body.get("names", {}).items()}
