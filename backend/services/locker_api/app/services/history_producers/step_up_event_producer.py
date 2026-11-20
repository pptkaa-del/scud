"""Публикация security-event в history_api для аудита 2FA-mismatch.

Зеркало ``auth_api.services.security_event_producer.SecurityEventProducer``
для пути ``command_2fa_mismatch`` (publish-and-forget HTTP POST). Отдельный
файл от ``security_event_producer.py``, который реализует существующий
queue-backed pattern для threshold/intrusion/tamper событий.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
import uuid

from shared.http import RetryHTTPClient, UpstreamUnavailableError
from shared.utils import body_sha256, compute_request_signature

logger = logging.getLogger(__name__)

_ENDPOINT = "/internal/api/v1/events/security"
# HTTP-метод подписываемого запроса (совпадает с self.request.method на
# стороне history_api InternalHmacMixin).
_METHOD = "POST"


class StepUpEventProducer:
    """HTTP-публишер step-up audit-событий в history_api.

    Используется только для ``command_2fa_mismatch`` в locker_api.
    Остальные ``command_2fa_*`` события публикует ``auth_api``.

    Каждый POST подписывается HMAC-ключом ``INTERNAL_HISTORY_HMAC_KEY``
    (SEC-05) и несёт продьюсер-генерируемый ``event_uid`` (ARC-02) для
    идемпотентности повторной доставки.
    """

    def __init__(
        self,
        base_url: str,
        http: RetryHTTPClient | None = None,
        hmac_key: bytes | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()
        self._hmac_key = hmac_key

    async def publish(
        self,
        *,
        event_type: str,
        severity: str,
        locker_id: str | None,
        metadata: dict[str, Any],
    ) -> None:
        """Отправить event в history_api. Никогда не падает наружу."""
        url = f"{self._base}{_ENDPOINT}"
        payload: dict[str, Any] = {
            "event_type": event_type,
            "severity": severity,
            "metadata": metadata,
            # ARC-02: один uid на publish() — стабилен между HTTP-ретраями
            # RetryHTTPClient внутри одного вызова.
            "event_uid": str(uuid.uuid4()),
        }
        if locker_id is not None:
            payload["locker_id"] = locker_id
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self._hmac_key is not None:
            timestamp = str(int(time.time()))
            headers["X-Internal-Timestamp"] = timestamp
            headers["X-Internal-Signature"] = compute_request_signature(
                timestamp=timestamp,
                method=_METHOD,
                path=_ENDPOINT,
                body_sha256_hex=body_sha256(body),
                key=self._hmac_key,
            )
        try:
            await self._http.fetch(
                url,
                method=_METHOD,
                body=body,
                headers=headers,
            )
        except UpstreamUnavailableError as exc:
            logger.warning(
                "history_api unreachable for security event %s: %s",
                event_type, exc,
            )
        except Exception as exc:
            # publish must never raise; пропавший аудит не должен ломать flow.
            logger.warning(
                "Failed to publish security event %s: %s", event_type, exc,
            )
