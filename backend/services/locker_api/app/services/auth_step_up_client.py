"""HTTP-клиент locker_api → auth_api для step-up OTP-челленджей.

Подписывает запросы HMAC-SHA256 ключом INTERNAL_STEP_UP_HMAC_KEY поверх
``timestamp + METHOD + path + sha256(body)`` (SEC-20: канон привязан к
методу и пути; generic-формат из shared.utils). На 4xx
verify возвращает структурированный ``StepUpVerifyError``; на 5xx/timeout —
поднимает ``UpstreamUnavailableError``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import time
from typing import Any

from tornado.httpclient import HTTPError

from shared.http import RetryHTTPClient, UpstreamUnavailableError
from shared.utils import body_sha256, compute_request_signature

logger = logging.getLogger(__name__)

_UPSTREAM = "auth_api"

# Подписываемые (method, path). Должны совпасть с self.request.method /
# self.request.path на стороне auth_api step-up handlers.
_METHOD = "POST"
_ISSUE_PATH = "/internal/api/v1/step-up/issue"
_VERIFY_PATH = "/internal/api/v1/step-up/verify"


@dataclass(frozen=True, slots=True)
class StepUpIssueResult:
    """Ответ ``/internal/api/v1/step-up/issue``.

    ``challenge_id`` равен None только в защитном крайнем случае: у пользователя
    нет ни телефона, ни привязки Max — доставить код второго фактора некуда.
    Практически недостижимо (``users.phone`` NOT NULL). Вызывающий handler
    разворачивает это в 401 для клиента.
    """

    challenge_id: str | None
    expires_at: str | None
    delivery_status: str  # 'sent' | 'queued_until_binding' | 'max_binding_required'


@dataclass(frozen=True, slots=True)
class StepUpVerifyResult:
    """Успешный ответ ``/internal/api/v1/step-up/verify``."""

    scope_kind: str
    scope_payload: dict[str, Any]


@dataclass(frozen=True, slots=True)
class StepUpVerifyError:
    """4xx-результат verify. ``status_code`` определяет, как handler ответит."""

    status_code: int
    detail: str


class AuthStepUpClient:
    """Async HTTP-клиент к auth_api для step-up endpoints."""

    def __init__(
        self,
        base_url: str,
        http: RetryHTTPClient | None = None,
        hmac_key: bytes | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()
        # Если ключ не задан — это **только** dev-сценарий. В production
        # main.py обязан передать bytes-ключ из секрета.
        self._hmac_key = hmac_key

    def _signed_headers(self, path: str, body: bytes) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._hmac_key is not None:
            ts = str(int(time.time()))
            sig = compute_request_signature(
                timestamp=ts,
                method=_METHOD,
                path=path,
                body_sha256_hex=body_sha256(body),
                key=self._hmac_key,
            )
            headers["X-Internal-Timestamp"] = ts
            headers["X-Internal-Signature"] = sig
        return headers

    async def issue(
        self,
        *,
        user_id: int,
        scope_kind: str,
        scope_payload: dict[str, Any],
        ttl: int = 300,
    ) -> StepUpIssueResult:
        """POST /internal/api/v1/step-up/issue."""
        url = f"{self._base}{_ISSUE_PATH}"
        body = json.dumps({
            "user_id": user_id,
            "scope_kind": scope_kind,
            "scope_payload": scope_payload,
            "ttl_seconds": ttl,
        }).encode("utf-8")

        try:
            resp = await self._http.fetch(
                url, method=_METHOD, body=body,
                headers=self._signed_headers(_ISSUE_PATH, body),
            )
        except HTTPError as exc:
            logger.error("auth_api /step-up/issue returned %s: %s", exc.code, exc)
            raise UpstreamUnavailableError(
                f"auth_api returned {exc.code}",
                upstream=_UPSTREAM,
                status=exc.code,
            ) from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            logger.error("auth_api /step-up/issue failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc

        if resp.code != 201:
            raise UpstreamUnavailableError(
                f"auth_api returned unexpected {resp.code}",
                upstream=_UPSTREAM,
            )
        payload = json.loads(resp.body or b"{}")
        if not isinstance(payload, dict):
            raise UpstreamUnavailableError(
                "auth_api returned non-dict body", upstream=_UPSTREAM,
            )
        challenge_id_raw = payload.get("challenge_id")
        expires_at_raw = payload.get("expires_at")
        delivery_status_raw = payload.get("delivery_status", "")
        return StepUpIssueResult(
            challenge_id=challenge_id_raw if isinstance(challenge_id_raw, str) else None,
            expires_at=expires_at_raw if isinstance(expires_at_raw, str) else None,
            delivery_status=str(delivery_status_raw),
        )

    async def verify(
        self,
        *,
        challenge_id: str,
        user_id: int,
        code: str,
    ) -> StepUpVerifyResult | StepUpVerifyError:
        """POST /internal/api/v1/step-up/verify.

        401/403 возвращаются как ``StepUpVerifyError`` (это not-availability
        ошибки — клиент должен их увидеть). 5xx и сетевые сбои —
        ``UpstreamUnavailableError``.
        """
        url = f"{self._base}{_VERIFY_PATH}"
        body = json.dumps({
            "challenge_id": challenge_id,
            "user_id": user_id,
            "code": code,
        }).encode("utf-8")

        try:
            resp = await self._http.fetch(
                url, method=_METHOD, body=body,
                headers=self._signed_headers(_VERIFY_PATH, body),
            )
        except HTTPError as exc:
            if exc.code in (401, 403):
                detail = ""
                try:
                    if exc.response is not None and exc.response.body:
                        parsed = json.loads(exc.response.body)
                        if isinstance(parsed, dict):
                            detail_val = parsed.get("detail", "")
                            detail = str(detail_val) if detail_val else ""
                except (ValueError, TypeError):
                    detail = ""
                return StepUpVerifyError(status_code=exc.code, detail=detail)
            logger.error("auth_api /step-up/verify returned %s: %s", exc.code, exc)
            raise UpstreamUnavailableError(
                f"auth_api returned {exc.code}",
                upstream=_UPSTREAM,
                status=exc.code,
            ) from exc
        except UpstreamUnavailableError:
            raise
        except Exception as exc:
            logger.error("auth_api /step-up/verify failed after retries: %s", exc)
            raise UpstreamUnavailableError(str(exc), upstream=_UPSTREAM) from exc

        if resp.code != 200:
            raise UpstreamUnavailableError(
                f"auth_api returned unexpected {resp.code}",
                upstream=_UPSTREAM,
            )
        payload = json.loads(resp.body or b"{}")
        if not isinstance(payload, dict):
            raise UpstreamUnavailableError(
                "auth_api returned non-dict body", upstream=_UPSTREAM,
            )
        scope_payload_raw = payload.get("scope_payload", {})
        scope_payload: dict[str, Any] = (
            scope_payload_raw if isinstance(scope_payload_raw, dict) else {}
        )
        return StepUpVerifyResult(
            scope_kind=str(payload.get("scope_kind", "")),
            scope_payload=scope_payload,
        )
