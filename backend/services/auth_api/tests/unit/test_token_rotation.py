"""Unit-тесты ротации opaque tokens.

Покрывает:
- verify создаёт token (token_repo.create вызван)
- access.py refresh: атомарная ротация (TokenRepository.rotate)
- revoked токен → 401 (get_by_hash фильтрует по revoked_at IS NULL)
- rotate возвращает None если old_token_hash уже отозван (race protection)
- revoke_by_user_id: SELECT COUNT + UPDATE паттерн в repo

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.auth_data.db.repositories.token_repository import TokenRepository
from app.handlers.access import AccessHandler
from app.handlers.verify import VerifyHandler
from app.services.otp import hash_otp as _hash_otp
from tests.unit.conftest import make_access_token, make_handler, make_otp


class TestTokenRotation(unittest.TestCase):

    # ------------------------------------------------------------------
    # 1. verify создаёт access token (token_repo.create вызван)
    # ------------------------------------------------------------------
    async def _token_created_on_verify(self) -> dict:
        code = "123456"
        otp = make_otp(otp_id=1, code_hash=_hash_otp(code))

        handler = make_handler(VerifyHandler, {
            "otp_max_attempts": 3,
            "access_token_ttl_hours": 24,
            "secure_cookies": False,
            "user_api_client": object(),
            "verify_rate_limit": 0,  # выключено для unit (db=object())
        }, db=object())
        handler.request.body = f'{{"phone":"+71112223344","code":"{code}"}}'.encode()

        captured: dict = {}

        async def fake_create(**kwargs):
            captured.update(kwargs)
            return make_access_token(
                user_id=kwargs["user_id"],
                token_hash=kwargs["token_hash"],
            )

        with patch(
            "app.handlers.verify.OtpRepository.get_active",
            new=AsyncMock(return_value=otp),
        ), patch(
            "app.handlers.verify.OtpRepository.reserve_attempt",
            new=AsyncMock(return_value={"attempts": 1, "code_hash": otp.code_hash}),
        ), patch(
            "app.handlers.verify.OtpRepository.consume",
            new=AsyncMock(return_value=True),
        ), patch.object(
            VerifyHandler, "_get_user_by_phone",
            new=AsyncMock(return_value={"user_id": 42, "name": "n", "role": "worker"}),
        ), patch(
            "app.handlers.verify.TokenRepository.create",
            new=AsyncMock(side_effect=fake_create),
        ), patch.object(
            VerifyHandler, "_set_access_cookie", return_value=None,
        ), patch.object(
            VerifyHandler, "write_json", return_value=None,
        ):
            await handler.post()
        return captured

    def test_token_created_on_verify(self):
        """verify успешно завершён → token_repository.create вызван с user_id и token_hash."""
        captured = asyncio.run(self._token_created_on_verify())
        self.assertEqual(captured.get("user_id"), 42)
        self.assertIsInstance(captured.get("token_hash"), str)
        # SHA-256 hex длина 64 символа
        self.assertEqual(len(captured["token_hash"]), 64)
        self.assertIn("expires_at", captured)

    # ------------------------------------------------------------------
    # 2. access.py: refresh выполняет атомарную ротацию через rotate()
    # ------------------------------------------------------------------
    async def _refresh_calls_rotate_atomically(self) -> dict:
        old_token = "old_raw_token_xxx"
        from shared.utils import hash_token
        old_hash = hash_token(old_token)
        stored = make_access_token(token_id=10, user_id=42, token_hash=old_hash)

        handler = make_handler(AccessHandler, {
            "access_token_ttl_hours": 24,
            "secure_cookies": False,
        }, db=object())

        handler.get_cookie = MagicMock(return_value=old_token)  # type: ignore[method-assign]

        rotate_mock = AsyncMock(return_value=stored)
        revoke_mock = AsyncMock(return_value=True)
        create_mock = AsyncMock(return_value=stored)

        with patch(
            "app.handlers.access.TokenRepository.get_by_hash",
            new=AsyncMock(return_value=stored),
        ), patch(
            "app.handlers.access.TokenRepository.rotate",
            new=rotate_mock,
        ), patch(
            "app.handlers.access.TokenRepository.revoke",
            new=revoke_mock,
        ), patch(
            "app.handlers.access.TokenRepository.create",
            new=create_mock,
        ), patch.object(
            AccessHandler, "_set_access_cookie", return_value=None,
        ), patch.object(
            AccessHandler, "write_json", return_value=None,
        ):
            await handler.post()

        return {
            "rotate_calls": rotate_mock.await_count,
            "rotate_kwargs": rotate_mock.await_args.kwargs if rotate_mock.await_args else None,
            "revoke_calls": revoke_mock.await_count,
            "create_calls": create_mock.await_count,
            "old_hash": old_hash,
        }

    def test_refresh_uses_atomic_rotate(self):
        """POST /access: вызывается rotate() (атомарно), отдельные revoke+create НЕ вызываются."""
        out = asyncio.run(self._refresh_calls_rotate_atomically())
        self.assertEqual(out["rotate_calls"], 1)
        self.assertEqual(out["revoke_calls"], 0)
        self.assertEqual(out["create_calls"], 0)
        kwargs = out["rotate_kwargs"]
        self.assertEqual(kwargs["old_token_hash"], out["old_hash"])
        # new hash != old hash (rotation)
        self.assertNotEqual(kwargs["new_token_hash"], out["old_hash"])
        self.assertEqual(len(kwargs["new_token_hash"]), 64)
        self.assertIn("expires_at", kwargs)

    # ------------------------------------------------------------------
    # 2b. access.py: rotate вернул None → 401 (race-protection)
    # ------------------------------------------------------------------
    async def _refresh_stale_token_returns_401(self) -> int:
        old_token = "old_raw_token_xxx"
        from shared.utils import hash_token
        old_hash = hash_token(old_token)
        stored = make_access_token(token_id=10, user_id=42, token_hash=old_hash)

        handler = make_handler(AccessHandler, {
            "access_token_ttl_hours": 24,
            "secure_cookies": False,
        }, db=object())
        handler.get_cookie = MagicMock(return_value=old_token)  # type: ignore[method-assign]

        captured: dict = {}

        def fake_unauth(detail: str) -> None:
            captured["status"] = 401

        with patch(
            "app.handlers.access.TokenRepository.get_by_hash",
            new=AsyncMock(return_value=stored),
        ), patch(
            "app.handlers.access.TokenRepository.rotate",
            new=AsyncMock(return_value=None),
        ), patch.object(
            AccessHandler, "write_unauthorized", side_effect=fake_unauth,
        ), patch.object(
            AccessHandler, "_set_access_cookie", return_value=None,
        ), patch.object(
            AccessHandler, "write_json", return_value=None,
        ):
            await handler.post()
        return captured.get("status", 0)

    def test_refresh_stale_rotate_returns_401(self):
        """Если rotate() вернул None (token уже отозван конкурентным запросом) → 401."""
        status = asyncio.run(self._refresh_stale_token_returns_401())
        self.assertEqual(status, 401)

    # ------------------------------------------------------------------
    # 3. revoked токен → 401 (get_by_hash вернул None)
    # ------------------------------------------------------------------
    async def _revoked_token_rejected(self) -> int:
        handler = make_handler(AccessHandler, {
            "access_token_ttl_hours": 24,
            "secure_cookies": False,
        }, db=object())
        handler.get_cookie = MagicMock(return_value="some_revoked_token")  # type: ignore[method-assign]

        captured: dict = {}

        def fake_unauth(detail: str) -> None:
            captured["status"] = 401

        # get_by_hash в SQL фильтрует revoked_at IS NULL → revoked токен → None
        with patch(
            "app.handlers.access.TokenRepository.get_by_hash",
            new=AsyncMock(return_value=None),
        ), patch.object(AccessHandler, "write_unauthorized", side_effect=fake_unauth):
            await handler.post()
        return captured.get("status", 0)

    def test_refresh_revoked_token_rejected(self):
        """access.post с revoked/expired token → 401 (get_by_hash вернул None)."""
        status = asyncio.run(self._revoked_token_rejected())
        self.assertEqual(status, 401)

    # ------------------------------------------------------------------
    # 4. revoke_by_user_id: COUNT + UPDATE паттерн
    # ------------------------------------------------------------------
    async def _revoke_by_user_id_invalidates_all(self) -> dict:
        """
        Проверяем поведение TokenRepository.revoke_by_user_id напрямую:
        один атомарный UPDATE ... RETURNING (COR-30); count = число
        возвращённых строк, без отдельного SELECT COUNT.
        """
        db = MagicMock()
        db.fetch_all = AsyncMock(return_value=[{"id": 1}, {"id": 2}, {"id": 3}])

        repo = TokenRepository(db)
        result = await repo.revoke_by_user_id(user_id=99)

        return {
            "result": result,
            "fetch_all_args": db.fetch_all.await_args,
        }

    def test_revoke_by_user_id_invalidates_all(self):
        """revoke_by_user_id: один UPDATE ... RETURNING (3 строки) → returns 3."""
        out = asyncio.run(self._revoke_by_user_id_invalidates_all())
        self.assertEqual(out["result"], 3)

        # Единственный запрос — write-side UPDATE ... RETURNING.
        update_sql = out["fetch_all_args"].args[0]
        update_params = out["fetch_all_args"].args[1]
        self.assertIn("UPDATE access_tokens", update_sql)
        self.assertIn("SET revoked_at = now()", update_sql)
        self.assertIn("revoked_at IS NULL", update_sql)
        self.assertIn("RETURNING", update_sql)
        self.assertNotIn("SELECT COUNT", update_sql)
        self.assertEqual(update_params, (99,))
        self.assertEqual(out["fetch_all_args"].kwargs.get("read_only"), False)

    async def _revoke_by_user_id_no_active(self) -> tuple[int, int]:
        """Когда активных токенов нет — UPDATE вернёт 0 строк, count=0."""
        db = MagicMock()
        db.fetch_all = AsyncMock(return_value=[])

        repo = TokenRepository(db)
        result = await repo.revoke_by_user_id(user_id=42)

        # Ровно один атомарный запрос; отдельного SELECT COUNT больше нет.
        return result, db.fetch_all.await_count

    def test_revoke_by_user_id_no_active_tokens_skips_update(self):
        """Нет активных токенов → count=0 из единственного UPDATE ... RETURNING."""
        result, fetch_all_calls = asyncio.run(self._revoke_by_user_id_no_active())
        self.assertEqual(result, 0)
        self.assertEqual(fetch_all_calls, 1)

    # ------------------------------------------------------------------
    # 5. TokenRepository.rotate: race-protection (CTE matched 0 rows)
    # ------------------------------------------------------------------
    async def _rotate_returns_none_when_already_revoked(self):
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=None)  # CTE matched 0 rows
        repo = TokenRepository(db)
        result = await repo.rotate(
            old_token_hash="old_hash",
            new_token_hash="new_hash",
            expires_at=datetime.utcnow() + timedelta(hours=1),
        )
        self.assertIsNone(result)
        # Убеждаемся, что SQL — атомарный CTE rotate, а не два запроса
        sql = db.fetch_one.await_args.args[0]
        self.assertIn("WITH revoked AS", sql)
        self.assertIn("INSERT INTO access_tokens", sql)

    def test_rotate_returns_none_when_old_token_already_revoked(self):
        """Rotate of stale token (already revoked) returns None — race protection."""
        asyncio.run(self._rotate_returns_none_when_already_revoked())

    async def _rotate_returns_token_on_success(self):
        from app.auth_data.db.models.tokens import AccessToken
        now = datetime.utcnow()
        row = {
            "id": 99,
            "user_id": 42,
            "token_hash": "new_hash",
            "expires_at": now + timedelta(hours=24),
            "revoked_at": None,
            "created_at": now,
            "user_agent": "ua",
            "ip_address": "127.0.0.1",
        }
        db = MagicMock()
        db.fetch_one = AsyncMock(return_value=row)
        repo = TokenRepository(db)
        result = await repo.rotate(
            old_token_hash="old_hash",
            new_token_hash="new_hash",
            expires_at=now + timedelta(hours=24),
            user_agent="ua",
            ip_address="127.0.0.1",
        )
        self.assertIsInstance(result, AccessToken)
        self.assertEqual(result.user_id, 42)
        self.assertEqual(result.token_hash, "new_hash")

    def test_rotate_returns_access_token_on_success(self):
        """Rotate возвращает AccessToken при успешной атомарной ротации."""
        asyncio.run(self._rotate_returns_token_on_success())


if __name__ == "__main__":
    unittest.main()
