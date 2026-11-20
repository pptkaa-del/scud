"""Unit-тесты RevokeUserTokensHandler (DELETE /api/v1/auth/users/{user_id}/tokens).

Покрывает все ветки revoke_user.py:
1. невалидный target_user_id из URL → 400
2. отсутствующий X-Remote-User → 401
3. нечисловой X-Remote-User → 401
4. UpstreamUnavailableError → 503
5. UserApiClient.get_user вернул None → 401
6. роль не ADMINISTRATOR → 403
7. админ → TokenRepository.revoke_by_user_id вызван, write_json({"revoked": N})

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio).
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from tornado.web import Finish

from app.handlers.revoke_user import RevokeUserTokensHandler
from shared.enums import UserRole
from shared.http import UpstreamUnavailableError
from tests.unit.conftest import _FakeRequest, make_handler


def _capture_responses(handler):
    """Подменяет write_*-helpers на лямбды, складывающие вызовы в dict.

    Возвращает dict с ключами: validation, unauthorized, forbidden,
    service_unavailable, json — каждый список аргументов.
    """
    captured: dict = {
        "validation": [],
        "unauthorized": [],
        "forbidden": [],
        "service_unavailable": [],
        "json": [],
    }
    handler.write_validation_error = lambda d: captured["validation"].append(d)  # type: ignore[assignment]
    handler.write_unauthorized = lambda d: captured["unauthorized"].append(d)  # type: ignore[assignment]
    handler.write_forbidden = lambda d: captured["forbidden"].append(d)  # type: ignore[assignment]
    handler.write_service_unavailable = lambda d: captured["service_unavailable"].append(d)  # type: ignore[assignment]
    handler.write_json = lambda payload: captured["json"].append(payload)  # type: ignore[assignment]
    # get_int_path_arg вызывает self.finish() перед raise Finish; в unit-стенде
    # (без полного Tornado-стека) подменяем на no-op, отмечающий _finished.
    def _finish(*_a, **_k):
        handler._finished = True
    handler.finish = _finish  # type: ignore[assignment]
    return captured


def _make(client_mock=None, headers=None) -> RevokeUserTokensHandler:
    settings = {"user_api_client": client_mock if client_mock is not None else MagicMock()}
    request = _FakeRequest(headers=headers or {}, method="DELETE",
                           uri="/api/v1/auth/users/42/tokens")
    return make_handler(RevokeUserTokensHandler, settings, request=request, db=object())


class TestRevokeUserTokensHandler(unittest.TestCase):

    # ---- 1. невалидный target_user_id ------------------------------------
    def test_invalid_target_user_id_returns_400(self):
        handler = _make(headers={"X-Remote-User": "1"})
        captured = _capture_responses(handler)

        # get_int_path_arg пишет 400 и поднимает Finish — handler прерывается.
        with self.assertRaises(Finish):
            asyncio.run(handler.delete("not-an-int"))

        self.assertEqual(len(captured["validation"]), 1)
        self.assertIn("user_id", captured["validation"][0].lower())
        self.assertEqual(captured["json"], [])

    # ---- 2. отсутствующий X-Remote-User ---------------------------------
    def test_missing_x_remote_user_returns_401(self):
        handler = _make(headers={})  # без заголовка
        captured = _capture_responses(handler)

        asyncio.run(handler.delete("42"))

        self.assertEqual(captured["unauthorized"], ["Authentication required"])
        self.assertEqual(captured["json"], [])

    # ---- 3. нечисловой X-Remote-User ------------------------------------
    def test_non_numeric_x_remote_user_returns_401(self):
        handler = _make(headers={"X-Remote-User": "abc"})
        captured = _capture_responses(handler)

        asyncio.run(handler.delete("42"))

        self.assertEqual(captured["unauthorized"], ["Invalid X-Remote-User header"])
        self.assertEqual(captured["json"], [])

    # ---- 4. UpstreamUnavailableError → 503 -------------------------------
    def test_user_api_unavailable_returns_503(self):
        client = MagicMock()
        client.get_user = AsyncMock(side_effect=UpstreamUnavailableError("boom", upstream="user_api"))
        handler = _make(client_mock=client, headers={"X-Remote-User": "1"})
        captured = _capture_responses(handler)

        asyncio.run(handler.delete("42"))

        self.assertEqual(len(captured["service_unavailable"]), 1)
        self.assertEqual(captured["json"], [])
        client.get_user.assert_awaited_once_with(1)

    # ---- 5. get_user вернул None → 401 ----------------------------------
    def test_caller_not_found_returns_401(self):
        client = MagicMock()
        client.get_user = AsyncMock(return_value=None)
        handler = _make(client_mock=client, headers={"X-Remote-User": "1"})
        captured = _capture_responses(handler)

        asyncio.run(handler.delete("42"))

        self.assertEqual(captured["unauthorized"], ["Could not verify caller identity"])
        self.assertEqual(captured["json"], [])

    # ---- 6. роль не ADMINISTRATOR → 403 ---------------------------------
    def test_non_admin_role_returns_403(self):
        client = MagicMock()
        client.get_user = AsyncMock(return_value={"id": 1, "role": UserRole.OPERATOR.value})
        handler = _make(client_mock=client, headers={"X-Remote-User": "1"})
        captured = _capture_responses(handler)

        with patch(
            "app.handlers.revoke_user.TokenRepository.revoke_by_user_id",
            new=AsyncMock(return_value=99),
        ) as revoke_mock:
            asyncio.run(handler.delete("42"))

        self.assertEqual(len(captured["forbidden"]), 1)
        self.assertEqual(captured["json"], [])
        revoke_mock.assert_not_awaited()

    def test_worker_role_returns_403(self):
        # Sanity-check ещё одной не-admin роли (OPERATOR покрыт выше).
        client = MagicMock()
        client.get_user = AsyncMock(return_value={"id": 1, "role": UserRole.WORKER.value})
        handler = _make(client_mock=client, headers={"X-Remote-User": "1"})
        captured = _capture_responses(handler)

        with patch(
            "app.handlers.revoke_user.TokenRepository.revoke_by_user_id",
            new=AsyncMock(return_value=0),
        ) as revoke_mock:
            asyncio.run(handler.delete("42"))

        self.assertEqual(len(captured["forbidden"]), 1)
        revoke_mock.assert_not_awaited()

    # ---- 7. админ → revoke_by_user_id вызван, ответ {"revoked": N} ------
    def test_admin_revokes_tokens_and_returns_count(self):
        client = MagicMock()
        client.get_user = AsyncMock(return_value={"id": 7, "role": UserRole.ADMINISTRATOR.value})
        handler = _make(client_mock=client, headers={"X-Remote-User": "7"})
        captured = _capture_responses(handler)

        with patch(
            "app.handlers.revoke_user.TokenRepository.revoke_by_user_id",
            new=AsyncMock(return_value=3),
        ) as revoke_mock:
            asyncio.run(handler.delete("42"))

        revoke_mock.assert_awaited_once_with(42)
        self.assertEqual(captured["json"], [{"revoked": 3}])
        self.assertEqual(captured["validation"], [])
        self.assertEqual(captured["unauthorized"], [])
        self.assertEqual(captured["forbidden"], [])
        self.assertEqual(captured["service_unavailable"], [])


if __name__ == "__main__":
    unittest.main()
