"""HTTP-клиент для интеграции с user_api.

Все вызовы идут через `RetryHTTPClient` (3 попытки + exp backoff).
4xx-ответы 401/404 интерпретируются как «нет данных» (None).
Иные 4xx и исчерпание retry на 5xx/network → `UpstreamUnavailableError`
с `upstream="user_api"` — вызывающий handler должен ответить 503.

Безопасность: запросы к ``/internal/auth/check`` подписываются HMAC-SHA256
generic-формой ``timestamp + METHOD + path + sha256(body)`` общим ключом
``INTERNAL_AUTH_HMAC_KEY``. user_api проверяет подпись и отклоняет
запросы, где она не совпадает, ``X-Internal-Timestamp`` старше 30 секунд
или подпись снята для другого method/path. Это защищает от злоумышленника,
перехватившего ``password_hash`` внутри backend_net: без ключа он не сможет
подменить подпись, а replay устаревает через 30 секунд и не переносится на
другой эндпойнт.

Для by-phone селектор лежит в query (``?phone=``), поэтому подпись снимается
с ``signed_path(path, qs)`` — канонизированный query входит в канон и подмену
``phone=`` внутри replay-окна отвергает user_api (SEC-10).
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urlencode

from tornado.httpclient import HTTPError

from shared.http import RetryHTTPClient, UpstreamUnavailableError
from shared.utils import body_sha256, compute_request_signature, signed_path

logger = logging.getLogger(__name__)

# Подписываемые (method, path) — должны побайтово совпасть с тем, что
# принимающий handler видит в self.request.method / self.request.path.
_AUTH_CHECK_METHOD = "POST"
_AUTH_CHECK_PATH = "/internal/auth/check"
_MAX_CONTEXT_METHOD = "GET"
# PII-read эндпойнты (SEC-10): подписываются ключом internal_user_read_hmac_key.
# Для by-phone селектор лежит в query (?phone=...) — подпись снимается с
# signed_path(path, qs), чтобы query вошёл в канон (user_api сверяет так же).
# Для by-id селектор в path — конкретный /internal/user/{id}, query нет.
_USER_READ_METHOD = "GET"
_BY_PHONE_PATH = "/internal/user/by-phone"
_BY_ID_PATH_PREFIX = "/internal/user/"




class UserApiClient:
    """Async HTTP-клиент к user_api для нужд auth_api."""

    def __init__(
        self,
        base_url: str,
        http: RetryHTTPClient | None = None,
        hmac_key: bytes | None = None,
        step_up_hmac_key: bytes | None = None,
        user_read_hmac_key: bytes | None = None,
    ):
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()
        # Если ключ не задан — это **только** dev-сценарий (например, юнит-тесты,
        # где user_api замокан и подпись не проверяется). В production main.py
        # обязан передать bytes-ключ из ENV.
        self._hmac_key = hmac_key
        # Отдельный HMAC-ключ для /internal/user/{id}/max-context. Разделён с
        # internal_auth_hmac_key — компрометация одного канала не открывает
        # доступа к другому inter-service path.
        self._step_up_hmac_key = step_up_hmac_key
        # Отдельный HMAC-ключ для PII-read эндпойнтов (by-phone, by-id) — SEC-10.
        self._user_read_hmac_key = user_read_hmac_key

    def _read_signed_headers(self, path: str) -> dict[str, str]:
        """X-Internal-* для GET PII-read запроса (пустое тело) над path.

        ``path`` для эндпойнтов с query (by-phone) обязан быть уже свёрнут
        через ``signed_path(path, qs)`` вызывающим — здесь он подписывается
        как есть, симметрично тому, что считает user_api. Возвращает пустой
        dict, если ключ не настроен (dev/CI fail-open).
        """
        if self._user_read_hmac_key is None:
            return {}
        timestamp = str(int(time.time()))
        signature = compute_request_signature(
            timestamp=timestamp,
            method=_USER_READ_METHOD,
            path=path,
            body_sha256_hex=body_sha256(b""),
            key=self._user_read_hmac_key,
        )
        return {
            "X-Internal-Timestamp": timestamp,
            "X-Internal-Signature": signature,
        }

    async def check_credentials(
        self, phone: str, password_hash: str
    ) -> dict[str, Any] | None:
        """POST /internal/auth/check.

        Returns:
            dict с полями user_id/require_2fa/name/role при 200,
            None при 401/404 (неверные креды / нет пользователя).

        Raises:
            UpstreamUnavailableError(upstream=user_api) при исчерпании retry или неожиданном 4xx.
        """
        url = f"{self._base}{_AUTH_CHECK_PATH}"
        body = json.dumps({"phone": phone, "password_hash": password_hash})

        headers: dict[str, str] = {"Content-Type": "application/json"}
        # Подпись добавляется только если ключ настроен. user_api ровно так же
        # пропускает запросы без подписи, если у него ключ не настроен — это
        # консистентный graceful-режим для dev/CI без секретов. В production
        # ключ должен быть выставлен **в обоих** сервисах. phone теперь входит
        # в канон только через sha256(body) — никакого field-injection.
        if self._hmac_key is not None:
            timestamp = str(int(time.time()))
            signature = compute_request_signature(
                timestamp=timestamp,
                method=_AUTH_CHECK_METHOD,
                path=_AUTH_CHECK_PATH,
                body_sha256_hex=body_sha256(body.encode("utf-8")),
                key=self._hmac_key,
            )
            headers["X-Internal-Timestamp"] = timestamp
            headers["X-Internal-Signature"] = signature

        try:
            # idempotent=True: /internal/auth/check — это read-only проверка
            # учётных данных (никакой мутации/побочного эффекта), несмотря на
            # POST. Повтор при таймауте/5xx безопасен и не плодит дубликатов,
            # поэтому сохраняем resilience для auth-critical пути (COR-11).
            resp = await self._http.fetch(
                url,
                method=_AUTH_CHECK_METHOD,
                body=body,
                headers=headers,
                idempotent=True,
            )
            parsed = json.loads(resp.body)
            if not isinstance(parsed, dict):
                logger.error(
                    "user_api /internal/auth/check returned non-dict body: %s",
                    type(parsed).__name__,
                )
                raise UpstreamUnavailableError(
                    "user_api returned non-dict body", upstream='user_api',
                )
            return parsed
        except HTTPError as exc:
            if exc.code in (401, 404):
                return None
            logger.error("user_api check_credentials returned %s: %s", exc.code, exc)
            raise UpstreamUnavailableError(f"user_api returned unexpected {exc.code}", upstream='user_api') from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            logger.error("user_api check_credentials failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream='user_api') from exc

    async def get_user_by_phone(self, phone: str) -> dict[str, Any] | None:
        """GET /internal/user/by-phone?phone=...

        Returns:
            dict ``{user_id, name, role}`` при 200, None при 404.

        Raises:
            UpstreamUnavailableError(upstream=user_api) при исчерпании retry или неожиданном 4xx.
        """
        # Строим query-строку через urlencode один раз — она идёт И в URL,
        # И в подпись. urlencode (не ручной quote) гарантирует, что wire-query
        # round-trip'ится через parse_qsl+urlencode на стороне user_api в ту же
        # каноническую строку → подписи signer и verifier сходятся.
        qs = urlencode({"phone": phone})
        url = f"{self._base}{_BY_PHONE_PATH}?{qs}"
        # Подпись снимается с signed_path(path, qs) — канонизированный query
        # входит в канон, так что подмена phone= внутри replay-окна отвергается
        # user_api (SEC-10). user_api сворачивает query тем же signed_path.
        headers = self._read_signed_headers(signed_path(_BY_PHONE_PATH, qs))
        try:
            resp = await self._http.fetch(url, method=_USER_READ_METHOD, headers=headers)
            data = json.loads(resp.body)
            if not isinstance(data, dict):
                logger.error(
                    "user_api /internal/user/by-phone returned non-dict body: %s",
                    type(data).__name__,
                )
                raise UpstreamUnavailableError(
                    "user_api returned non-dict body", upstream='user_api',
                )
            return data
        except HTTPError as exc:
            if exc.code in (401, 404):
                return None
            logger.error(
                "user_api get_user_by_phone returned %s: %s", exc.code, exc
            )
            raise UpstreamUnavailableError(f"user_api returned unexpected {exc.code}", upstream='user_api') from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            logger.error(
                "user_api get_user_by_phone failed after retries: %s", exc
            )
            raise UpstreamUnavailableError(str(exc), upstream='user_api') from exc

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        """GET /internal/user/{user_id}.

        Returns:
            dict с полями пользователя (включая role) при 200, None при 404.

        Raises:
            UpstreamUnavailableError(upstream=user_api) при исчерпании retry или неожиданном 4xx.
        """
        path = f"{_BY_ID_PATH_PREFIX}{user_id}"
        url = f"{self._base}{path}"
        # id входит в подписываемый path — подписываем ровно тот path, что
        # запрашиваем (user_api сверяет против self.request.path).
        headers = self._read_signed_headers(path)
        try:
            resp = await self._http.fetch(url, method=_USER_READ_METHOD, headers=headers)
            parsed = json.loads(resp.body)
            if not isinstance(parsed, dict):
                logger.error(
                    "user_api /internal/user/%s returned non-dict body: %s",
                    user_id, type(parsed).__name__,
                )
                raise UpstreamUnavailableError(
                    "user_api returned non-dict body", upstream='user_api',
                )
            return parsed
        except HTTPError as exc:
            if exc.code in (401, 404):
                return None
            logger.error("user_api get_user returned %s: %s", exc.code, exc)
            raise UpstreamUnavailableError(f"user_api returned unexpected {exc.code}", upstream='user_api') from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            logger.error("user_api get_user failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream='user_api') from exc

    async def get_max_context(self, user_id: int) -> dict[str, Any] | None:
        """GET /internal/user/{id}/max-context.

        Возвращает ``{"require_2fa": bool, "max_user_id": int | None,
        "phone": str}`` или ``None`` если пользователь не найден. ``phone``
        используется step-up flow для SMS-primary доставки OTP command-2FA.
        На 5xx — поднимает ``UpstreamUnavailableError`` (как и check_credentials).
        """
        # id входит в подписываемый путь — подписываем ровно тот конкретный
        # path, что запрашиваем (user_api проверяет против self.request.path).
        path = f"/internal/user/{user_id}/max-context"
        url = f"{self._base}{path}"
        headers: dict[str, str] = {"Accept": "application/json"}
        if self._step_up_hmac_key is not None:
            timestamp = str(int(time.time()))
            signature = compute_request_signature(
                timestamp=timestamp,
                method=_MAX_CONTEXT_METHOD,
                path=path,
                body_sha256_hex=body_sha256(b""),
                key=self._step_up_hmac_key,
            )
            headers["X-Internal-Timestamp"] = timestamp
            headers["X-Internal-Signature"] = signature
        try:
            resp = await self._http.fetch(
                url, method=_MAX_CONTEXT_METHOD, headers=headers,
            )
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise UpstreamUnavailableError(
                f"user_api returned {exc.code}",
                upstream='user_api',
                status=exc.code,
            ) from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            raise UpstreamUnavailableError(str(exc), upstream='user_api') from exc
        if resp.code != 200:
            raise UpstreamUnavailableError(
                f"user_api returned unexpected {resp.code}",
                upstream='user_api',
            )
        parsed = json.loads(resp.body or b"{}")
        if not isinstance(parsed, dict):
            raise UpstreamUnavailableError(
                "user_api returned non-dict body", upstream='user_api',
            )
        return parsed
