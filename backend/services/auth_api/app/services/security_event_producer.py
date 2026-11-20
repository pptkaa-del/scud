"""Публикация security-event в history_api для аудита 2FA-челленджей.

Эндпойнт ``POST /internal/api/v1/events/security`` закрыт снаружи через
Apache ``<Location /internal>``; между сервисами защищён HMAC-подписью
``X-Internal-Timestamp`` + ``X-Internal-Signature`` ключом
``INTERNAL_HISTORY_HMAC_KEY`` (SEC-05). Каждое событие несёт
продьюсер-генерируемый ``event_uid`` (ARC-02) для идемпотентности.
Любая сетевая ошибка проглатывается: пропавший аудит не должен блокировать
ответ клиенту в auth-критичном flow.
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


class SecurityEventProducer:
    """HTTP-публишер событий безопасности в history_api.

    Используется для аудита step-up 2FA: issued / verified / failed.
    `command_2fa_mismatch` публикуется зеркальным продьюсером в locker_api.
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
        """Отправить event в history_api. Никогда не падает наружу.

        OTP-код НЕ должен попадать в ``metadata`` — это правило enforced на
        стороне вызывающего; здесь логика только сериализует и шлёт.
        """
        url = f"{self._base}{_ENDPOINT}"
        payload: dict[str, Any] = {
            "event_type": event_type,
            "severity": severity,
            "metadata": metadata,
            # ARC-02: один uid на publish() — стабилен между HTTP-ретраями.
            # history_api дедуплицирует по event_uid (ON CONFLICT DO NOTHING),
            # поэтому ретрай (idempotent=True ниже) безопасен: повторная
            # доставка не создаёт дубль события.
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
                # Ретрай безопасен: event_uid + ON CONFLICT DO NOTHING на
                # стороне history_api дедуплицируют повторную доставку. Без
                # idempotent=True RetryHTTPClient (COR-10/11) не ретраит POST
                # на ambiguous 5xx/timeout и аудит теряется на первом сбое.
                idempotent=True,
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
