"""HTTP-клиент user_api → auth_api для отзыва сессий (SEC-14).

После успешной смены учётных данных (пароль / переключение 2FA) user_api
дёргает ``POST /internal/api/v1/sessions/revoke`` в auth_api, чтобы отозвать
все живые access tokens пользователя — скомпрометированная сессия не должна
пережить сброс пароля.

Запрос подписывается HMAC-SHA256 ключом ``INTERNAL_SESSION_REVOKE_HMAC_KEY``
поверх ``timestamp + METHOD + path + sha256(body)`` (SEC-20: канон привязан
к методу и пути). Если ключ не задан — запрос уходит без подписи (dev/CI),
auth_api в таком же режиме его пропустит.

Семантика best-effort: смена учётных данных уже закоммичена в user_api,
поэтому ошибку отзыва клиент пробрасывает наверх — вызывающий handler ловит
её и лишь логирует WARNING, не проваливая основную операцию.
"""

from __future__ import annotations

import json
import logging
import time

from shared.http import RetryHTTPClient
from shared.utils import body_sha256, compute_request_signature

logger = logging.getLogger(__name__)

# Подписываемые (method, path) — должны побайтово совпасть с тем, что
# принимающий SessionRevokeHandler видит в self.request.method/path.
_REVOKE_METHOD = "POST"
_REVOKE_PATH = "/internal/api/v1/sessions/revoke"

_HDR_TIMESTAMP = "X-Internal-Timestamp"
_HDR_SIGNATURE = "X-Internal-Signature"


class AuthApiSessionClient:
    """Async клиент auth_api для отзыва сессий пользователя."""

    def __init__(
        self,
        base_url: str,
        http: RetryHTTPClient | None = None,
        hmac_key: bytes | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._http = http or RetryHTTPClient()
        # Если ключ не задан — dev/CI без секретов. auth_api в таком же режиме
        # пропустит запрос без подписи. В production ключ ОБЯЗАН быть установлен
        # в обоих сервисах.
        self._hmac_key = hmac_key

    async def revoke_user_sessions(self, user_id: int) -> int:
        """POST /internal/api/v1/sessions/revoke {"user_id": id}.

        Returns:
            Число отозванных токенов (поле ``revoked`` ответа), 0 если ответ
            без него.

        Raises:
            Прокидывает любое исключение транспортного слоя (после исчерпания
            retry) — вызывающий обязан обработать best-effort.
        """
        url = f"{self._base}{_REVOKE_PATH}"
        body = json.dumps({"user_id": user_id}).encode("utf-8")

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._hmac_key is not None:
            # Timestamp считаем ОДИН раз и используем и в заголовке, и в подписи.
            timestamp = str(int(time.time()))
            signature = compute_request_signature(
                timestamp=timestamp,
                method=_REVOKE_METHOD,
                path=_REVOKE_PATH,
                body_sha256_hex=body_sha256(body),
                key=self._hmac_key,
            )
            headers[_HDR_TIMESTAMP] = timestamp
            headers[_HDR_SIGNATURE] = signature

        # idempotent=True: revoke — это идемпотентный UPDATE (повторный отзыв
        # уже отозванного токена ничего не ломает), повтор POST безопасен.
        resp = await self._http.fetch(
            url,
            method=_REVOKE_METHOD,
            body=body,
            headers=headers,
            idempotent=True,
        )
        try:
            payload = json.loads(resp.body or b"{}")
        except (ValueError, TypeError):
            return 0
        if isinstance(payload, dict):
            revoked = payload.get("revoked", 0)
            if isinstance(revoked, int) and not isinstance(revoked, bool):
                return revoked
        return 0
