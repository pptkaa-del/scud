"""Unit tests для AuthStepUpClient: issue/verify HMAC-headers + ответ-мэппинг."""
from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.auth_step_up_client import (
    AuthStepUpClient,
    StepUpIssueResult,
    StepUpVerifyError,
    StepUpVerifyResult,
)
from shared.http import UpstreamUnavailableError


def _make_response(status_code: int, body: dict) -> MagicMock:
    resp = MagicMock()
    resp.code = status_code
    resp.body = json.dumps(body).encode()
    return resp


class TestAuthStepUpClientIssue(unittest.TestCase):
    """Issue: 201 happy path, null challenge, 5xx → unavailable."""

    def test_issue_happy_path_returns_challenge_id(self) -> None:
        async def _run() -> None:
            client = AuthStepUpClient(
                base_url="http://auth:8003",
                hmac_key=b"test-key-32-bytes-padding-padding",
            )
            mock_resp = _make_response(201, {
                "challenge_id": "uuid-1",
                "expires_at": "2026-05-28T12:00:00Z",
                "delivery_status": "sent",
            })
            captured: dict[str, object] = {}

            async def _fetch(url: str, method: str = "GET", body=None, headers=None, **kwargs):
                captured["url"] = url
                captured["method"] = method
                captured["body"] = body
                captured["headers"] = headers
                return mock_resp

            with patch.object(client._http, "fetch", AsyncMock(side_effect=_fetch)):
                result = await client.issue(
                    user_id=42,
                    scope_kind="locker_command",
                    scope_payload={"locker_id": "x", "command_type": "open"},
                    ttl=300,
                )

            self.assertIsInstance(result, StepUpIssueResult)
            self.assertEqual(result.challenge_id, "uuid-1")
            self.assertEqual(result.delivery_status, "sent")
            self.assertEqual(captured["method"], "POST")
            headers = captured["headers"]
            assert isinstance(headers, dict)
            # HMAC-заголовки должны быть выставлены, когда ключ настроен.
            self.assertIn("X-Internal-Timestamp", headers)
            self.assertIn("X-Internal-Signature", headers)
            self.assertEqual(headers["Content-Type"], "application/json")
            body = captured["body"]
            assert isinstance(body, bytes)
            parsed = json.loads(body)
            self.assertEqual(parsed["user_id"], 42)
            self.assertEqual(parsed["scope_kind"], "locker_command")
            self.assertEqual(parsed["ttl_seconds"], 300)

        asyncio.run(_run())

    def test_issue_null_challenge_id_max_binding_required(self) -> None:
        async def _run() -> None:
            client = AuthStepUpClient(base_url="http://auth:8003")
            mock_resp = _make_response(201, {
                "challenge_id": None,
                "expires_at": None,
                "delivery_status": "max_binding_required",
            })
            with patch.object(client._http, "fetch", AsyncMock(return_value=mock_resp)):
                result = await client.issue(
                    user_id=42, scope_kind="locker_command", scope_payload={},
                )
            self.assertIsInstance(result, StepUpIssueResult)
            self.assertIsNone(result.challenge_id)
            self.assertEqual(result.delivery_status, "max_binding_required")

        asyncio.run(_run())

    def test_issue_no_hmac_key_omits_signature_headers(self) -> None:
        async def _run() -> None:
            client = AuthStepUpClient(base_url="http://auth:8003", hmac_key=None)
            mock_resp = _make_response(201, {
                "challenge_id": "x", "expires_at": "t", "delivery_status": "sent",
            })
            captured: dict[str, object] = {}

            async def _fetch(url: str, method: str = "GET", body=None, headers=None, **kwargs):
                captured["headers"] = headers
                return mock_resp

            with patch.object(client._http, "fetch", AsyncMock(side_effect=_fetch)):
                await client.issue(
                    user_id=1, scope_kind="locker_command", scope_payload={},
                )

            headers = captured["headers"]
            assert isinstance(headers, dict)
            self.assertNotIn("X-Internal-Timestamp", headers)
            self.assertNotIn("X-Internal-Signature", headers)

        asyncio.run(_run())

    def test_issue_5xx_raises_unavailable(self) -> None:
        async def _run() -> None:
            from tornado.httpclient import HTTPError
            client = AuthStepUpClient(base_url="http://auth:8003")
            with patch.object(
                client._http, "fetch", AsyncMock(side_effect=HTTPError(500, "boom")),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.issue(
                    user_id=1, scope_kind="locker_command", scope_payload={},
                )

        asyncio.run(_run())


class TestAuthStepUpClientVerify(unittest.TestCase):
    """Verify: 200 happy, 401 → StepUpVerifyError, 403 → error, 5xx → unavailable."""

    def test_verify_happy_path_returns_scope_payload(self) -> None:
        async def _run() -> None:
            client = AuthStepUpClient(base_url="http://auth:8003")
            mock_resp = _make_response(200, {
                "scope_kind": "locker_command",
                "scope_payload": {"locker_id": "x", "command_type": "open"},
            })
            with patch.object(client._http, "fetch", AsyncMock(return_value=mock_resp)):
                result = await client.verify(
                    challenge_id="cid", user_id=1, code="123456",
                )
            self.assertIsInstance(result, StepUpVerifyResult)
            assert isinstance(result, StepUpVerifyResult)  # narrow for mypy
            self.assertEqual(result.scope_kind, "locker_command")
            self.assertEqual(result.scope_payload["command_type"], "open")

        asyncio.run(_run())

    def test_verify_401_returns_step_up_error_with_detail(self) -> None:
        async def _run() -> None:
            from tornado.httpclient import HTTPError
            client = AuthStepUpClient(base_url="http://auth:8003")
            err_resp = MagicMock()
            err_resp.body = json.dumps({"detail": "Invalid code"}).encode()
            with patch.object(
                client._http, "fetch",
                AsyncMock(side_effect=HTTPError(401, "no", response=err_resp)),
            ):
                result = await client.verify(
                    challenge_id="cid", user_id=1, code="wrong",
                )
            self.assertIsInstance(result, StepUpVerifyError)
            assert isinstance(result, StepUpVerifyError)  # narrow for mypy
            self.assertEqual(result.status_code, 401)
            self.assertEqual(result.detail, "Invalid code")

        asyncio.run(_run())

    def test_verify_403_returns_step_up_error(self) -> None:
        async def _run() -> None:
            from tornado.httpclient import HTTPError
            client = AuthStepUpClient(base_url="http://auth:8003")
            err_resp = MagicMock()
            err_resp.body = json.dumps({"detail": "Wrong user"}).encode()
            with patch.object(
                client._http, "fetch",
                AsyncMock(side_effect=HTTPError(403, "no", response=err_resp)),
            ):
                result = await client.verify(
                    challenge_id="cid", user_id=999, code="123456",
                )
            self.assertIsInstance(result, StepUpVerifyError)
            assert isinstance(result, StepUpVerifyError)
            self.assertEqual(result.status_code, 403)

        asyncio.run(_run())

    def test_verify_5xx_raises_unavailable(self) -> None:
        async def _run() -> None:
            from tornado.httpclient import HTTPError
            client = AuthStepUpClient(base_url="http://auth:8003")
            with patch.object(
                client._http, "fetch", AsyncMock(side_effect=HTTPError(503, "no")),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.verify(
                    challenge_id="cid", user_id=1, code="123456",
                )

        asyncio.run(_run())

    def test_verify_network_error_raises_unavailable(self) -> None:
        async def _run() -> None:
            client = AuthStepUpClient(base_url="http://auth:8003")
            with patch.object(
                client._http, "fetch",
                AsyncMock(side_effect=OSError("connection refused")),
            ), self.assertRaises(UpstreamUnavailableError):
                await client.verify(
                    challenge_id="cid", user_id=1, code="123456",
                )

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
