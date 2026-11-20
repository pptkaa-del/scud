"""Unit-тесты InternalMaxBindingHandler — PATCH /internal/user/by-invite."""

from datetime import datetime, timedelta
import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.testing import AsyncHTTPTestCase

from app.main import make_app
from app.user_data.db.repositories.max_invite_token import MaxInviteToken

_HMAC_KEY = b"unit-binding-hmac"

# Канон подписи (SEC-20): timestamp\nMETHOD\npath\nsha256(body). Все тесты
# шлют POST на /internal/user/by-invite.
_METHOD = "POST"
_PATH = "/internal/user/by-invite"


def _sign(body: bytes, key: bytes, ts: str | None = None) -> dict[str, str]:
    ts = ts or str(int(time.time()))
    body_hex = hashlib.sha256(body).hexdigest()
    msg = f"{ts}\n{_METHOD}\n{_PATH}\n{body_hex}".encode()
    sig = hmac.new(key, msg, hashlib.sha256).hexdigest()
    return {
        "X-Internal-Timestamp": ts,
        "X-Internal-Signature": sig,
        "Content-Type": "application/json",
    }


def _make_token(user_id: int = 42, token: str = "valid-token") -> MaxInviteToken:
    return MaxInviteToken(
        token=token,
        user_id=user_id,
        expires_at=datetime.now() + timedelta(hours=1),
        used_at=None,
        created_at=datetime.now(),
    )


class _Base(AsyncHTTPTestCase):
    def get_app(self):
        self.db_mock = MagicMock()
        # Сохраним original env и подставим ключ binding через main.py-settings.
        with patch.dict(
            "os.environ",
            {"INTERNAL_MAX_BINDING_HMAC_KEY": _HMAC_KEY.decode()},
            clear=False,
        ):
            return make_app(self.db_mock, cache_invalidator=None)


class TestInternalMaxBinding(_Base):

    def test_400_on_missing_fields(self):
        body = json.dumps({"invite_token": "x"}).encode()
        response = self.fetch(
            "/internal/user/by-invite",
            method="POST",
            body=body,
            headers=_sign(body, _HMAC_KEY),
        )
        self.assertEqual(response.code, 400)

    def test_400_on_oversized_invite_token_before_db(self):
        """SEC-21: токен длиннее MAX_INVITE_TOKEN_LEN → 400 ДО обращения к БД."""
        from app.handlers.internal_max_binding import MAX_INVITE_TOKEN_LEN

        oversized = "A" * (MAX_INVITE_TOKEN_LEN + 1)
        body = json.dumps(
            {"invite_token": oversized, "max_user_id": 99},
        ).encode()
        with patch(
            "app.handlers.internal_max_binding.MaxInviteTokenRepository",
        ) as tok_cls:
            tok_inst = tok_cls.return_value
            tok_inst.consume_active = AsyncMock(return_value=None)
            response = self.fetch(
                "/internal/user/by-invite",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 400)
            # Раздутый токен не должен доходить до lookup в БД.
            tok_inst.consume_active.assert_not_awaited()

    def test_401_on_bad_signature(self):
        body = json.dumps({"invite_token": "x", "max_user_id": 99}).encode()
        response = self.fetch(
            "/internal/user/by-invite",
            method="POST",
            body=body,
            headers=_sign(body, b"wrong-key"),
        )
        self.assertEqual(response.code, 401)

    def test_404_on_unknown_token(self):
        body = json.dumps({"invite_token": "unknown", "max_user_id": 99}).encode()
        with patch(
            "app.handlers.internal_max_binding.MaxInviteTokenRepository",
        ) as tok_cls:
            tok_inst = tok_cls.return_value
            tok_inst.consume_active = AsyncMock(return_value=None)
            response = self.fetch(
                "/internal/user/by-invite",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 404)

    def test_409_on_max_user_id_already_bound(self):
        body = json.dumps({"invite_token": "valid", "max_user_id": 99}).encode()
        with patch(
            "app.handlers.internal_max_binding.MaxInviteTokenRepository",
        ) as tok_cls, patch(
            "app.handlers.internal_max_binding.UserRepository",
        ) as user_cls:
            tok_cls.return_value.consume_active = AsyncMock(return_value=_make_token())
            user_cls.return_value.set_max_user_id = AsyncMock(return_value=False)
            response = self.fetch(
                "/internal/user/by-invite",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 409)

    def test_200_on_successful_binding(self):
        body = json.dumps({"invite_token": "valid", "max_user_id": 99}).encode()
        with patch(
            "app.handlers.internal_max_binding.MaxInviteTokenRepository",
        ) as tok_cls, patch(
            "app.handlers.internal_max_binding.UserRepository",
        ) as user_cls:
            tok_cls.return_value.consume_active = AsyncMock(
                return_value=_make_token(user_id=42),
            )
            user_cls.return_value.set_max_user_id = AsyncMock(return_value=True)
            response = self.fetch(
                "/internal/user/by-invite",
                method="POST",
                body=body,
                headers=_sign(body, _HMAC_KEY),
            )
            self.assertEqual(response.code, 200)
            payload = json.loads(response.body)
            self.assertEqual(payload["user_id"], 42)

    def test_401_on_expired_timestamp(self):
        body = json.dumps({"invite_token": "valid", "max_user_id": 99}).encode()
        ts = str(int(time.time()) - 120)
        response = self.fetch(
            "/internal/user/by-invite",
            method="POST",
            body=body,
            headers=_sign(body, _HMAC_KEY, ts=ts),
        )
        self.assertEqual(response.code, 401)
