"""HTTP-клиент notification_api → user_api `/internal/user/by-invite`.

Используется MaxBotPoller'ом после события `bot_started`: invite-токен из
``start=<token>`` deep-link + числовой max_user_id отправляются в user_api,
который атомарно проставляет users.max_user_id и помечает токен used_at.

Запросы подписываются HMAC-SHA256 ключом ``INTERNAL_MAX_BINDING_HMAC_KEY``
(отдельным от ``INTERNAL_OTP_HMAC_KEY`` — компрометация одного канала не
открывает доступ ко второму).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
import logging
import time

from tornado.httpclient import HTTPError

from shared.http import RetryHTTPClient, UpstreamUnavailableError
from shared.utils import body_sha256, compute_request_signature

logger = logging.getLogger(__name__)


_UPSTREAM = "user_api"

# Подписываемые (method, path) — совпадают с self.request.method /
# self.request.path на стороне InternalMaxBindingHandler.
_BIND_METHOD = "POST"
_BIND_PATH = "/internal/user/by-invite"


class BindMaxResult(str, Enum):
    BOUND = "bound"               # 200 — успешная привязка, user_id известен.
    TOKEN_INVALID = "invalid"     # 404 — токен неизвестен/истёк/использован.
    CONFLICT = "conflict"         # 409 — max_user_id уже привязан к другому.


@dataclass
class BindMaxResponse:
    result: BindMaxResult
    user_id: int | None = None


class UserApiBindingClient:
    """Тонкий HMAC-клиент к user_api для привязки Max-аккаунта."""

    def __init__(
        self,
        base_url: str,
        http: RetryHTTPClient | None = None,
        hmac_key: bytes | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()
        self._hmac_key = hmac_key

    async def bind_max_user(
        self, *, invite_token: str, max_user_id: int,
    ) -> BindMaxResponse:
        """POST /internal/user/by-invite.

        Returns:
            BindMaxResponse(BOUND, user_id=<int>) при 200.
            BindMaxResponse(TOKEN_INVALID) при 404.
            BindMaxResponse(CONFLICT) при 409.

        Raises:
            UpstreamUnavailableError: 5xx, network, неожиданный 4xx.
        """
        url = f"{self._base}{_BIND_PATH}"
        body = json.dumps({
            "invite_token": invite_token,
            "max_user_id": max_user_id,
        }).encode("utf-8")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._hmac_key is not None:
            ts = str(int(time.time()))
            sig = compute_request_signature(
                timestamp=ts,
                method=_BIND_METHOD,
                path=_BIND_PATH,
                body_sha256_hex=body_sha256(body),
                key=self._hmac_key,
            )
            headers["X-Internal-Timestamp"] = ts
            headers["X-Internal-Signature"] = sig

        try:
            resp = await self._http.fetch(
                url, method=_BIND_METHOD, body=body, headers=headers,
            )
        except HTTPError as exc:
            if exc.code == 404:
                return BindMaxResponse(BindMaxResult.TOKEN_INVALID)
            if exc.code == 409:
                return BindMaxResponse(BindMaxResult.CONFLICT)
            logger.error("user_api /internal/user/by-invite returned %s", exc.code)
            raise UpstreamUnavailableError(
                f"user_api returned {exc.code}",
                upstream=_UPSTREAM,
                status=exc.code,
            ) from exc
        except Exception as exc:
            logger.error("user_api bind_max_user failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc

        if resp.code != 200:
            raise UpstreamUnavailableError(
                f"user_api returned unexpected {resp.code}",
                upstream=_UPSTREAM,
                status=resp.code,
            )
        try:
            payload = json.loads(resp.body or b"{}")
        except (ValueError, TypeError) as exc:
            raise UpstreamUnavailableError(
                f"user_api returned non-JSON 200: {exc}", upstream=_UPSTREAM,
            ) from exc
        user_id = payload.get("user_id") if isinstance(payload, dict) else None
        if not isinstance(user_id, int):
            raise UpstreamUnavailableError(
                "user_api /internal/user/by-invite 200 missing user_id",
                upstream=_UPSTREAM,
            )
        return BindMaxResponse(BindMaxResult.BOUND, user_id=user_id)
