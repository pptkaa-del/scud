"""GET /internal/user/{id}/max-context.

HMAC-подписанный read-only endpoint: возвращает ``require_2fa`` + ``max_user_id``
+ ``phone`` конкретного пользователя для step-up flow в auth_api. ``phone``
даёт auth_api SMS-primary доставку OTP command-2FA, когда max_user_id ещё не
привязан. Отдельно от полного ``/internal/auth/check`` (тот идёт по phone +
password_hash и тяжелее).
"""
from __future__ import annotations

import logging

from app.user_data.db.repositories.user import UserRepository
from shared.auth import ProblemDetailsHandler
from shared.utils import body_sha256, verify_request_signature

logger = logging.getLogger(__name__)


class InternalMaxContextHandler(ProblemDetailsHandler):
    """GET /internal/user/{id}/max-context."""

    def _verify_signature(self) -> bool:
        hmac_key = self.settings.get("internal_step_up_hmac_key")
        if hmac_key is None:
            return True
        ts = self.request.headers.get("X-Internal-Timestamp", "")
        sig = self.request.headers.get("X-Internal-Signature", "")
        return verify_request_signature(
            signature=sig,
            timestamp=ts,
            method=self.request.method or "",
            path=self.request.path,
            body_sha256_hex=body_sha256(b""),
            key=hmac_key,
        )

    async def get(self, user_id: str) -> None:
        if not self._verify_signature():
            self.write_unauthorized("Invalid or expired signature")
            return
        uid = self.get_int_path_arg(user_id, "user_id")
        repo = UserRepository(self.db)
        ctx = await repo.get_max_context(uid)
        if ctx is None:
            self.set_status(404)
            self.finish()
            return
        self.write_json({
            "require_2fa": bool(ctx["require_2fa"]),
            "max_user_id": ctx["max_user_id"],
            "phone": ctx["phone"],
        })
