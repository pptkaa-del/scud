"""HMAC-проверка подписи на internal-эндпойнтах history_api (SEC-05).

Все три internal-канала записи (security events, телеметрия, медиа) закрыты
снаружи через Apache ``<Location /internal> Require all denied``, но внутри
``backend_net`` ранее были доступны без подписи. Любой скомпрометированный
контейнер мог подделать или зафлудить AUDIT-трейл (включая ``command_2fa_*``).

Подпись считается над ``timestamp + METHOD + path + body_sha256(raw body)``
через generic-хелпер ``shared.utils.compute_request_signature`` — тот же
формат, что у ``/internal/api/v1/otp/send`` (notification_api) и
``/internal/auth/check`` (user_api). Привязка к method+path (SEC-20) не даёт
переиграть подпись против другого эндпойнта. Работает для любого тела,
включая multipart (подписывается весь raw-body запроса, до парсинга form-data).

Ключ: ``INTERNAL_HISTORY_HMAC_KEY`` (consistent с остальными ``INTERNAL_*_HMAC_KEY``),
читается через ``read_secret`` в ``main.py`` и кладётся в ``Application.settings``
под ключом ``internal_history_hmac_key``.

Fail-open posture: если ключ не сконфигурирован (dev/CI без секрета) —
проверка пропускается, ровно как у ``InternalOtpSendHandler`` /
``InternalAuthCheckHandler``. Перевод в fail-closed — известный follow-up
(PR-I/H), синхронный для всех HMAC-эндпойнтов сразу.
"""

from __future__ import annotations

import logging

from shared.utils import body_sha256, verify_request_signature

logger = logging.getLogger(__name__)

#: Имя ключа в ``Application.settings`` (см. history_api/main.py).
HMAC_SETTINGS_KEY = "internal_history_hmac_key"

_TIMESTAMP_HEADER = "X-Internal-Timestamp"
_SIGNATURE_HEADER = "X-Internal-Signature"


class InternalHmacMixin:
    """Mixin для проверки HMAC-подписи на internal write-эндпойнтах.

    Потребитель обязан быть Tornado RequestHandler (есть ``self.settings``,
    ``self.request``). Метод ``verify_internal_hmac`` возвращает ``True`` если
    подпись валидна ЛИБО ключ не сконфигурирован (fail-open).
    """

    def verify_internal_hmac(self) -> bool:
        """Сравнить X-Internal-* с подписью над ``body_sha256(raw body)``.

        Если ключ не настроен (dev/CI без секрета) — пропустить проверку.
        Никогда не кидает наверх: любые ошибки парсинга → ``False`` внутри
        ``verify_request_signature``.
        """
        hmac_key = self.settings.get(HMAC_SETTINGS_KEY)  # type: ignore[attr-defined]
        if not hmac_key:
            return True
        request = self.request  # type: ignore[attr-defined]
        headers = request.headers
        timestamp = headers.get(_TIMESTAMP_HEADER, "")
        signature = headers.get(_SIGNATURE_HEADER, "")
        body = request.body or b""
        key = hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
        return verify_request_signature(
            signature=signature,
            timestamp=timestamp,
            method=request.method or "",
            path=request.path,
            body_sha256_hex=body_sha256(body),
            key=key,
        )
