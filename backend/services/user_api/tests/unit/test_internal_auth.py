"""Unit-тесты для internal endpoints user_api (handlers/internal_auth.py).

Покрывает три handler'а:
- InternalAuthCheckHandler (POST /internal/auth/check) — phone+password_hash → 200/400/401
- InternalUserByPhoneHandler (GET /internal/user/by-phone) — phone → 200/400/404
- InternalUserByIdHandler (GET /internal/user/{id}) — int id → 200/400/404

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).

Handler'ы наследуются от JSONHandler и НЕ требуют X-Remote-User (закрыты
Apache снаружи). Мокаем UserRepository.get_by_phone/.get_by_id/.get_auth_record
+ bcrypt.checkpw, статус и тело ответа перехватываем через patch.object на
методах handler'а (set_status, finish, write_json).
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import time
from typing import Any, ClassVar, Optional
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.handlers.internal_auth import (
    InternalAuthCheckHandler,
    InternalUserByIdHandler,
    InternalUserByPhoneHandler,
)
from app.user_data.db.models.user import User
from shared.enums import UserRole, UserState
from shared.utils import body_sha256, compute_request_signature, signed_path
from tests.unit.conftest import _FakeRequest, make_handler

# (method, path) подписываемого /internal/auth/check — совпадает с тем, что
# handler видит в request.method / request.path.
_CHECK_METHOD = "POST"
_CHECK_PATH = "/internal/auth/check"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_PHONE = "+71112223344"
_PASSWORD_HASH = "a" * 64  # SHA-256 hex длина — 64 символа
_BCRYPT_HASH = "$2b$12$" + "x" * 53  # фиктивный bcrypt-hash, поведение мокаем
_HMAC_KEY = b"unit-test-internal-auth-key"


def _make_user(
    *,
    user_id: int = 42,
    phone: str = _PHONE,
    full_name: str = "Иван Иванов",
    state: UserState = UserState.ACTIVE,
    role: UserRole = UserRole.WORKER,
) -> User:
    """Сконструировать User dataclass для возврата из repo-mock."""
    now = datetime.utcnow()
    return User(
        id=user_id,
        phone=phone,
        full_name=full_name,
        contractor_id=None,
        contractor_name=None,
        created_at=now,
        created_by=None,
        updated_at=now,
        updated_by=None,
        state=state,
        role=role,
        expire_date=None,
    )


def _capture(handler) -> dict:
    """Перехват set_status/finish/write_json для проверки результата.

    Возвращает dict с ключами: status, finished, body. Заменяет методы
    handler'а замыканиями, чтобы не вызывать настоящий Tornado-стек
    (он требует HTTPConnection и пр.).
    """
    captured: dict[str, Any] = {"status": None, "finished": False, "body": None}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_finish(chunk: Any = None) -> None:
        captured["finished"] = True
        if chunk is not None:
            captured["body"] = chunk

    def fake_write_json(data: Any, status_code: int = 200) -> None:
        captured["status"] = status_code
        captured["body"] = data
        handler._status_code = status_code

    handler.set_status = fake_set_status  # type: ignore[assignment]
    handler.finish = fake_finish  # type: ignore[assignment]
    handler.write_json = fake_write_json  # type: ignore[assignment]
    return captured


def _make_check_handler(
    body: bytes = b"",
    headers: dict | None = None,
    settings: dict | None = None,
) -> InternalAuthCheckHandler:
    request = _FakeRequest(
        method="POST",
        uri="/internal/auth/check",
        body=body,
        headers=headers,
    )
    return make_handler(
        InternalAuthCheckHandler,
        settings=settings or {},
        request=request,
        db=object(),
    )


def _default_body() -> bytes:
    return (
        b'{"phone":"' + _PHONE.encode() + b'",'
        b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
    )


def _signed_headers(
    body: bytes | None = None,
    key: bytes = _HMAC_KEY,
    timestamp: str | None = None,
    method: str = _CHECK_METHOD,
    path: str = _CHECK_PATH,
) -> dict:
    """Сгенерировать валидные X-Internal-Timestamp + X-Internal-Signature.

    Подпись считается над method+path+sha256(body) — generic-форма SEC-20.
    """
    ts = timestamp if timestamp is not None else str(int(time.time()))
    sig = compute_request_signature(
        timestamp=ts,
        method=method,
        path=path,
        body_sha256_hex=body_sha256(body if body is not None else _default_body()),
        key=key,
    )
    return {
        "X-Internal-Timestamp": ts,
        "X-Internal-Signature": sig,
    }


def _make_by_phone_handler(
    query: dict | None = None,
    headers: dict | None = None,
    settings: dict | None = None,
) -> InternalUserByPhoneHandler:
    qa: dict = {}
    if query:
        for k, v in query.items():
            qa[k] = [v.encode("utf-8") if isinstance(v, str) else v]
    request = _FakeRequest(
        method="GET",
        uri="/internal/user/by-phone",
        query_arguments=qa,
        headers=headers,
    )
    return make_handler(
        InternalUserByPhoneHandler,
        settings=settings or {},
        request=request,
        db=object(),
    )


def _make_by_id_handler(
    uri: str = "/internal/user/42",
    headers: dict | None = None,
    settings: dict | None = None,
) -> InternalUserByIdHandler:
    request = _FakeRequest(method="GET", uri=uri, headers=headers)
    return make_handler(
        InternalUserByIdHandler,
        settings=settings or {},
        request=request,
        db=object(),
    )


# ---------------------------------------------------------------------------
# InternalAuthCheckHandler
# ---------------------------------------------------------------------------


class TestInternalAuthCheck(unittest.TestCase):
    """POST /internal/auth/check — phone + password_hash → user_id/role/2fa или 401."""

    # --- 1. Невалидный JSON ------------------------------------------------

    def test_invalid_json_body_returns_400(self):
        async def run() -> dict:
            handler = _make_check_handler(body=b"not a json {")
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)
        self.assertTrue(cap["finished"])

    # --- 2. Отсутствует phone ----------------------------------------------

    def test_missing_phone_returns_400(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)

    # --- 3. Отсутствует password_hash --------------------------------------

    def test_missing_password_hash_returns_400(self):
        async def run() -> dict:
            handler = _make_check_handler(body=b'{"phone":"' + _PHONE.encode() + b'"}')
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)

    # --- 4. Пользователь не найден ----------------------------------------

    def test_user_not_found_returns_401(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"phone":"' + _PHONE.encode() + b'",'
                     b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=None),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value=None),
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- 5. Пользователь в неактивном state -------------------------------

    def _run_check_with_state(self, state: UserState) -> dict:
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"phone":"' + _PHONE.encode() + b'",'
                     b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            user = _make_user(state=state)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value={
                    "password_hash": _BCRYPT_HASH,
                    "require_2fa": False,
                }),
            ):
                await handler.post()
            return cap

        return asyncio.run(run())

    def test_inactive_user_deactivated_returns_401(self):
        cap = self._run_check_with_state(UserState.DEACTIVATED)
        self.assertEqual(cap["status"], 401)

    def test_inactive_user_deleted_returns_401(self):
        cap = self._run_check_with_state(UserState.DELETED)
        self.assertEqual(cap["status"], 401)

    # --- 6. auth_record отсутствует ---------------------------------------

    def test_missing_auth_record_returns_401(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"phone":"' + _PHONE.encode() + b'",'
                     b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            user = _make_user(state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value=None),
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- 7. bcrypt.checkpw кидает исключение → 401 ------------------------

    def test_bcrypt_checkpw_raises_returns_401(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"phone":"' + _PHONE.encode() + b'",'
                     b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            user = _make_user(state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value={
                    "password_hash": "invalid-bcrypt-format",
                    "require_2fa": False,
                }),
            ), patch(
                "app.handlers.internal_auth.bcrypt.checkpw",
                side_effect=ValueError("Invalid salt"),
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- 8. bcrypt.checkpw возвращает False (неверный пароль) -------------

    def test_bcrypt_wrong_password_returns_401(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"phone":"' + _PHONE.encode() + b'",'
                     b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            user = _make_user(state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value={
                    "password_hash": _BCRYPT_HASH,
                    "require_2fa": False,
                }),
            ), patch(
                "app.handlers.internal_auth.bcrypt.checkpw",
                return_value=False,
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- 9. Happy path (require_2fa=False) --------------------------------

    def test_happy_path_no_2fa_returns_200(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"phone":"' + _PHONE.encode() + b'",'
                     b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            user = _make_user(
                user_id=42, full_name="Иван Иванов",
                state=UserState.ACTIVE, role=UserRole.WORKER,
            )
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value={
                    "password_hash": _BCRYPT_HASH,
                    "require_2fa": False,
                }),
            ), patch(
                "app.handlers.internal_auth.bcrypt.checkpw",
                return_value=True,
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertIsInstance(cap["body"], dict)
        self.assertEqual(cap["body"]["user_id"], 42)
        self.assertEqual(cap["body"]["require_2fa"], False)
        self.assertEqual(cap["body"]["name"], "Иван Иванов")
        # role сериализуется через str() — для StrEnum возвращает значение строки
        self.assertEqual(cap["body"]["role"], str(UserRole.WORKER))

    # --- 10. Happy path (require_2fa=True) --------------------------------

    def test_happy_path_with_2fa_returns_200(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=b'{"phone":"' + _PHONE.encode() + b'",'
                     b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            cap = _capture(handler)
            user = _make_user(user_id=7, role=UserRole.ADMINISTRATOR)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value={
                    "password_hash": _BCRYPT_HASH,
                    "require_2fa": True,
                }),
            ), patch(
                "app.handlers.internal_auth.bcrypt.checkpw",
                return_value=True,
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"]["user_id"], 7)
        self.assertEqual(cap["body"]["require_2fa"], True)
        self.assertEqual(cap["body"]["role"], str(UserRole.ADMINISTRATOR))


# ---------------------------------------------------------------------------
# HMAC-подпись запросов (settings["internal_auth_hmac_key"] выставлен)
# ---------------------------------------------------------------------------


class TestInternalAuthCheckSignature(unittest.TestCase):
    """Когда ``internal_auth_hmac_key`` задан, handler требует валидную подпись.

    Это режим production: docker-compose ставит ``INTERNAL_AUTH_HMAC_KEY`` для
    обоих сервисов; main.py user_api переносит ключ в ``application.settings``.
    """

    _BODY = (
        b'{"phone":"' + _PHONE.encode() + b'",'
        b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
    )

    def _settings(self) -> dict:
        return {"internal_auth_hmac_key": _HMAC_KEY}

    # --- Запрос без подписи отвергается ---------------------------------
    def test_missing_signature_header_returns_401(self):
        async def run() -> dict:
            handler = _make_check_handler(
                body=self._BODY, headers={}, settings=self._settings()
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)
        # Не падать в БД — отказ должен быть до get_by_phone.
        self.assertTrue(cap["finished"])

    # --- Невалидная подпись -------------------------------------------------
    def test_signature_mismatch_returns_401(self):
        async def run() -> dict:
            headers = _signed_headers()
            headers["X-Internal-Signature"] = "0" * 64
            handler = _make_check_handler(
                body=self._BODY, headers=headers, settings=self._settings()
            )
            cap = _capture(handler)
            # Repository не должен даже вызываться — подпись отсекает раньше.
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- Устаревший timestamp ---------------------------------------------
    def test_stale_timestamp_returns_401(self):
        async def run() -> dict:
            old_ts = str(int(time.time()) - 60)  # 60s назад > 30s окна
            headers = _signed_headers(timestamp=old_ts)
            handler = _make_check_handler(
                body=self._BODY, headers=headers, settings=self._settings()
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- Не тот ключ -------------------------------------------------------
    def test_wrong_key_returns_401(self):
        async def run() -> dict:
            headers = _signed_headers(key=b"other-key")
            handler = _make_check_handler(
                body=self._BODY, headers=headers, settings=self._settings()
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- Tampered body (phone в теле не совпадает с подписью) ----------
    def test_tampered_phone_returns_401(self):
        async def run() -> dict:
            # Подписали правильный _PHONE — а в теле другой.
            headers = _signed_headers()
            tampered_body = (
                b'{"phone":"+79999999999",'
                b'"password_hash":"' + _PASSWORD_HASH.encode() + b'"}'
            )
            handler = _make_check_handler(
                body=tampered_body, headers=headers, settings=self._settings()
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- Подпись снята для другого path (SEC-20) → 401 -----------------
    def test_signature_for_other_path_returns_401(self):
        async def run() -> dict:
            # Валидная подпись, но для ДРУГОГО пути — handler видит
            # request.path=/internal/auth/check и подпись не сходится.
            headers = _signed_headers(body=self._BODY, path="/internal/user/by-invite")
            handler = _make_check_handler(
                body=self._BODY, headers=headers, settings=self._settings()
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    # --- Happy path: валидная подпись + правильный пароль -------------
    def test_valid_signature_happy_path_returns_200(self):
        async def run() -> dict:
            headers = _signed_headers()
            handler = _make_check_handler(
                body=self._BODY, headers=headers, settings=self._settings()
            )
            cap = _capture(handler)
            user = _make_user(state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ), patch(
                "app.handlers.internal_auth.UserRepository.get_auth_record",
                new=AsyncMock(return_value={
                    "password_hash": _BCRYPT_HASH,
                    "require_2fa": False,
                }),
            ), patch(
                "app.handlers.internal_auth.bcrypt.checkpw",
                return_value=True,
            ):
                await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"]["user_id"], 42)


# ---------------------------------------------------------------------------
# InternalUserByPhoneHandler
# ---------------------------------------------------------------------------


class TestInternalUserByPhone(unittest.TestCase):
    """GET /internal/user/by-phone?phone=... → user_id или 400/404."""

    def test_empty_phone_returns_400(self):
        async def run() -> dict:
            # без query_arguments — get_query_argument вернёт default=""
            handler = _make_by_phone_handler(query=None)
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)

    def test_user_not_found_returns_404(self):
        async def run() -> dict:
            handler = _make_by_phone_handler(query={"phone": _PHONE})
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=None),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 404)

    def test_user_inactive_returns_404(self):
        async def run() -> dict:
            handler = _make_by_phone_handler(query={"phone": _PHONE})
            cap = _capture(handler)
            user = _make_user(state=UserState.DEACTIVATED)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 404)

    def test_happy_path_returns_200(self):
        async def run() -> dict:
            handler = _make_by_phone_handler(query={"phone": _PHONE})
            cap = _capture(handler)
            user = _make_user(user_id=99, state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(
            cap["body"],
            {"user_id": 99, "name": "Иван Иванов", "role": str(UserRole.WORKER)},
        )


# ---------------------------------------------------------------------------
# InternalUserByIdHandler
# ---------------------------------------------------------------------------


class TestInternalUserById(unittest.TestCase):
    """GET /internal/user/{id} → {"id":..., "role":...} или 400/404."""

    def test_non_numeric_user_id_returns_400(self):
        async def run() -> dict:
            handler = _make_by_id_handler()
            cap = _capture(handler)
            await handler.get("not-a-number")
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 400)

    def test_user_not_found_returns_404(self):
        async def run() -> dict:
            handler = _make_by_id_handler()
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_id",
                new=AsyncMock(return_value=None),
            ):
                await handler.get("42")
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 404)

    def test_happy_path_returns_200(self):
        async def run() -> dict:
            handler = _make_by_id_handler()
            cap = _capture(handler)
            user = _make_user(user_id=42, role=UserRole.OPERATOR)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_id",
                new=AsyncMock(return_value=user),
            ):
                await handler.get("42")
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"], {"id": 42, "role": str(UserRole.OPERATOR)})


# ---------------------------------------------------------------------------
# PII-read HMAC-подпись (settings["internal_user_read_hmac_key"] выставлен)
# ---------------------------------------------------------------------------

_READ_KEY = b"unit-test-user-read-key"
_BY_PHONE_PATH = "/internal/user/by-phone"
_BY_ID_PATH = "/internal/user/42"
_NAMES_PATH = "/internal/users/names"


def _read_signed_headers(
    path: str,
    key: bytes = _READ_KEY,
    timestamp: str | None = None,
    method: str = "GET",
) -> dict:
    """X-Internal-* для GET-запроса (пустое тело) над конкретным path."""
    ts = timestamp if timestamp is not None else str(int(time.time()))
    sig = compute_request_signature(
        timestamp=ts,
        method=method,
        path=path,
        body_sha256_hex=body_sha256(b""),
        key=key,
    )
    return {"X-Internal-Timestamp": ts, "X-Internal-Signature": sig}


class TestInternalUserByPhoneSignature(unittest.TestCase):
    """GET /internal/user/by-phone требует подпись когда ключ задан (SEC-10)."""

    def _settings(self) -> dict:
        return {"internal_user_read_hmac_key": _READ_KEY}

    def test_unsigned_returns_401(self):
        async def run() -> dict:
            handler = _make_by_phone_handler(
                query={"phone": _PHONE}, headers={}, settings=self._settings(),
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)
        self.assertTrue(cap["finished"])

    def test_valid_signature_returns_200(self):
        async def run() -> dict:
            headers = _read_signed_headers(_BY_PHONE_PATH)
            handler = _make_by_phone_handler(
                query={"phone": _PHONE}, headers=headers, settings=self._settings(),
            )
            cap = _capture(handler)
            user = _make_user(user_id=99, state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(
            cap["body"],
            {"user_id": 99, "name": "Иван Иванов", "role": str(UserRole.WORKER)},
        )

    def test_signature_for_other_path_returns_401(self):
        async def run() -> dict:
            # Подпись валидна, но для другого path — handler видит
            # request.path=/internal/user/by-phone и подпись не сходится.
            headers = _read_signed_headers("/internal/users/names")
            handler = _make_by_phone_handler(
                query={"phone": _PHONE}, headers=headers, settings=self._settings(),
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_wrong_key_returns_401(self):
        async def run() -> dict:
            headers = _read_signed_headers(_BY_PHONE_PATH, key=b"other-key")
            handler = _make_by_phone_handler(
                query={"phone": _PHONE}, headers=headers, settings=self._settings(),
            )
            cap = _capture(handler)
            await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_no_key_configured_skips_verification(self):
        # Ключ не задан → fail-open (dev/CI). Подпись не требуется.
        async def run() -> dict:
            handler = _make_by_phone_handler(query={"phone": _PHONE}, settings={})
            cap = _capture(handler)
            user = _make_user(user_id=5, state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(
            cap["body"],
            {"user_id": 5, "name": "Иван Иванов", "role": str(UserRole.WORKER)},
        )


class TestInternalUserByIdSignature(unittest.TestCase):
    """GET /internal/user/{id} требует подпись конкретного path (SEC-10)."""

    def _settings(self) -> dict:
        return {"internal_user_read_hmac_key": _READ_KEY}

    def test_unsigned_returns_401(self):
        async def run() -> dict:
            handler = _make_by_id_handler(headers={}, settings=self._settings())
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_id",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get("42")
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_valid_signature_returns_200(self):
        async def run() -> dict:
            headers = _read_signed_headers(_BY_ID_PATH)
            handler = _make_by_id_handler(headers=headers, settings=self._settings())
            cap = _capture(handler)
            user = _make_user(user_id=42, role=UserRole.OPERATOR)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_id",
                new=AsyncMock(return_value=user),
            ):
                await handler.get("42")
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"], {"id": 42, "role": str(UserRole.OPERATOR)})

    def test_signature_for_other_id_returns_401(self):
        async def run() -> dict:
            # Подпись снята для /internal/user/7, а запрашиваем /internal/user/42.
            headers = _read_signed_headers("/internal/user/7")
            handler = _make_by_id_handler(headers=headers, settings=self._settings())
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_id",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get("42")
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)


class TestInternalUserNamesSignature(unittest.TestCase):
    """GET /internal/users/names требует подпись когда ключ задан (SEC-10)."""

    def _settings(self) -> dict:
        return {"internal_user_read_hmac_key": _READ_KEY}

    def _make_names_handler(self, headers=None, settings=None, ids: str = "1,2"):
        request = _FakeRequest(
            method="GET",
            uri="/internal/users/names",
            query_arguments={"ids": [ids.encode("utf-8")]},
            headers=headers,
        )
        from app.handlers.internal_auth import InternalUserNamesHandler
        return make_handler(
            InternalUserNamesHandler,
            settings=settings or {},
            request=request,
            db=object(),
        )

    def test_unsigned_returns_401(self):
        async def run() -> dict:
            handler = self._make_names_handler(headers={}, settings=self._settings())
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_valid_signature_returns_200(self):
        async def run() -> dict:
            headers = _read_signed_headers(_NAMES_PATH)
            handler = self._make_names_handler(headers=headers, settings=self._settings())
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(return_value={1: "Иванов", 2: "Петров"}),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(cap["body"], {"names": {"1": "Иванов", "2": "Петров"}})

    def test_signature_for_other_path_returns_401(self):
        async def run() -> dict:
            headers = _read_signed_headers("/internal/user/by-phone")
            handler = self._make_names_handler(headers=headers, settings=self._settings())
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)


# ---------------------------------------------------------------------------
# Query-bound PII-read HMAC (SEC-10): by-phone / names несут селектор в query,
# и теперь query свёрнут в подписываемый path через signed_path. Здесь URI
# хендлера содержит реальную query-строку, чтобы request.query был непустой и
# verifier сворачивал её в канон.
# ---------------------------------------------------------------------------


def _make_by_phone_handler_with_query(
    wire_query: str,
    headers: dict | None = None,
    settings: dict | None = None,
) -> InternalUserByPhoneHandler:
    """by-phone handler, у которого URI содержит реальную query-строку.

    query_arguments выводим из wire_query, чтобы get_query_argument отработал.
    """
    from urllib.parse import parse_qs

    qa: dict = {}
    for k, vals in parse_qs(wire_query, keep_blank_values=True).items():
        qa[k] = [v.encode("utf-8") for v in vals]
    request = _FakeRequest(
        method="GET",
        uri=f"/internal/user/by-phone?{wire_query}",
        query_arguments=qa,
        headers=headers,
    )
    return make_handler(
        InternalUserByPhoneHandler,
        settings=settings or {},
        request=request,
        db=object(),
    )


def _make_names_handler_with_query(
    wire_query: str,
    headers: dict | None = None,
    settings: dict | None = None,
):
    from urllib.parse import parse_qs

    from app.handlers.internal_auth import InternalUserNamesHandler

    qa: dict = {}
    for k, vals in parse_qs(wire_query, keep_blank_values=True).items():
        qa[k] = [v.encode("utf-8") for v in vals]
    request = _FakeRequest(
        method="GET",
        uri=f"/internal/users/names?{wire_query}",
        query_arguments=qa,
        headers=headers,
    )
    return make_handler(
        InternalUserNamesHandler,
        settings=settings or {},
        request=request,
        db=object(),
    )


class TestInternalUserByPhoneQueryBound(unittest.TestCase):
    """by-phone: query (phone=) теперь входит в подпись через signed_path (SEC-10)."""

    _SETTINGS: ClassVar[dict[str, bytes]] = {"internal_user_read_hmac_key": _READ_KEY}
    _WIRE_QUERY = "phone=%2B71112223344"  # urlencode({'phone': '+71112223344'})

    def test_signature_without_query_rejected(self):
        """Подпись по старому канону (path БЕЗ query) → 401."""
        async def run() -> dict:
            # Старый стиль: подпись над bare path, query НЕ свёрнут.
            headers = _read_signed_headers(_BY_PHONE_PATH)
            handler = _make_by_phone_handler_with_query(
                self._WIRE_QUERY, headers=headers, settings=self._SETTINGS,
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_query_bound_signature_accepted(self):
        """Подпись над signed_path(path, query) → 200."""
        async def run() -> dict:
            headers = _read_signed_headers(
                signed_path(_BY_PHONE_PATH, self._WIRE_QUERY)
            )
            handler = _make_by_phone_handler_with_query(
                self._WIRE_QUERY, headers=headers, settings=self._SETTINGS,
            )
            cap = _capture(handler)
            user = _make_user(user_id=99, state=UserState.ACTIVE)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(return_value=user),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(
            cap["body"],
            {"user_id": 99, "name": "Иван Иванов", "role": str(UserRole.WORKER)},
        )

    def test_swapped_query_value_rejected(self):
        """Подпись снята для phone=A, а запрос несёт phone=B → 401."""
        async def run() -> dict:
            # Подписали канонический query для +71112223344.
            headers = _read_signed_headers(
                signed_path(_BY_PHONE_PATH, self._WIRE_QUERY)
            )
            # Запрос несёт другой phone в query — verifier сворачивает уже B.
            handler = _make_by_phone_handler_with_query(
                "phone=%2B79999999999", headers=headers, settings=self._SETTINGS,
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_by_phone",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)


class TestInternalUserNamesQueryBound(unittest.TestCase):
    """names: query (ids=) теперь входит в подпись через signed_path (SEC-10)."""

    _SETTINGS: ClassVar[dict[str, bytes]] = {"internal_user_read_hmac_key": _READ_KEY}
    _WIRE_QUERY = "ids=1%2C2%2C3"  # urlencode({'ids': '1,2,3'})

    def test_signature_without_query_rejected(self):
        async def run() -> dict:
            headers = _read_signed_headers(_NAMES_PATH)  # bare path, старый стиль
            handler = _make_names_handler_with_query(
                self._WIRE_QUERY, headers=headers, settings=self._SETTINGS,
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_query_bound_signature_accepted(self):
        async def run() -> dict:
            headers = _read_signed_headers(
                signed_path(_NAMES_PATH, self._WIRE_QUERY)
            )
            handler = _make_names_handler_with_query(
                self._WIRE_QUERY, headers=headers, settings=self._SETTINGS,
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(return_value={1: "Иванов", 2: "Петров", 3: "Сидоров"}),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 200)
        self.assertEqual(
            cap["body"],
            {"names": {"1": "Иванов", "2": "Петров", "3": "Сидоров"}},
        )

    def test_swapped_query_value_rejected(self):
        async def run() -> dict:
            headers = _read_signed_headers(
                signed_path(_NAMES_PATH, self._WIRE_QUERY)
            )
            # Запрос несёт другой ids в query → 401.
            handler = _make_names_handler_with_query(
                "ids=9%2C9%2C9", headers=headers, settings=self._SETTINGS,
            )
            cap = _capture(handler)
            with patch(
                "app.handlers.internal_auth.UserRepository.get_full_names_by_ids",
                new=AsyncMock(side_effect=AssertionError("DB не должен вызываться")),
            ):
                await handler.get()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)


if __name__ == "__main__":
    unittest.main()
