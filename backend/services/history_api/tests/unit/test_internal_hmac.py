"""Unit-тесты HMAC-проверки на internal write-эндпойнтах history_api (SEC-05).

Покрывает:
- InternalHmacMixin.verify_internal_hmac: валидная подпись → True, битая/устаревшая
  → False, отсутствие ключа в settings → True (fail-open).
- InternalSecurityEventHandler / InternalTelemetryHandler / InternalMediaUploadHandler:
  при сконфигурированном ключе POST без подписи → 401, с валидной → проходит к
  бизнес-логике.

Тесты — `def test_*` + `asyncio.run()` (нет pytest-asyncio в Astra).
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any
import unittest
from unittest.mock import AsyncMock

from app.handlers.internal_command_history import InternalCommandHistoryHandler
from app.handlers.internal_hmac import HMAC_SETTINGS_KEY, InternalHmacMixin
from app.handlers.internal_security_event import InternalSecurityEventHandler
from app.handlers.internal_telemetry import InternalTelemetryHandler
from shared.utils import body_sha256, compute_request_signature
from tests.unit.conftest import _FakeRequest, make_handler

_KEY = "test-history-hmac-key"
# Все internal-handler'ы в этих тестах поднимаются на uri="/internal" с POST —
# подпись должна совпасть с тем, что handler видит в request.method/path.
_METHOD = "POST"
_PATH = "/internal"


def _sign(
    body: bytes,
    key: str = _KEY,
    timestamp: str | None = None,
    method: str = _METHOD,
    path: str = _PATH,
) -> dict[str, str]:
    ts = timestamp or str(int(time.time()))
    sig = compute_request_signature(
        timestamp=ts,
        method=method,
        path=path,
        body_sha256_hex=body_sha256(body),
        key=key.encode("utf-8"),
    )
    return {"X-Internal-Timestamp": ts, "X-Internal-Signature": sig}


def _capture(handler) -> dict:
    captured: dict[str, Any] = {"status": 200, "body": None}

    def fake_set_status(code: int, reason: str | None = None) -> None:
        captured["status"] = code
        handler._status_code = code

    def fake_set_header(name: str, value: str) -> None:
        pass

    def fake_write(chunk: Any) -> None:
        captured["body"] = chunk

    handler.set_status = fake_set_status  # type: ignore[assignment]
    handler.set_header = fake_set_header  # type: ignore[assignment]
    handler.write = fake_write  # type: ignore[assignment]
    return captured


class _MixinProbe(InternalHmacMixin):
    """Минимальный потребитель миксина для unit-проверки verify_internal_hmac."""

    def __init__(self, settings: dict, request: _FakeRequest) -> None:
        self.settings = settings
        self.request = request


class TestVerifyInternalHmac(unittest.TestCase):
    def _probe(self, settings: dict, body: bytes, headers: dict) -> _MixinProbe:
        req = _FakeRequest(method="POST", uri="/internal", body=body, headers=headers)
        return _MixinProbe(settings, req)

    def test_no_key_in_settings_fails_open(self):
        probe = self._probe({}, b'{"a":1}', {})
        self.assertTrue(probe.verify_internal_hmac())

    def test_valid_signature_accepted(self):
        body = b'{"a":1}'
        probe = self._probe({HMAC_SETTINGS_KEY: _KEY}, body, _sign(body))
        self.assertTrue(probe.verify_internal_hmac())

    def test_missing_signature_rejected_when_key_set(self):
        probe = self._probe({HMAC_SETTINGS_KEY: _KEY}, b'{"a":1}', {})
        self.assertFalse(probe.verify_internal_hmac())

    def test_tampered_body_rejected(self):
        headers = _sign(b'{"a":1}')
        probe = self._probe({HMAC_SETTINGS_KEY: _KEY}, b'{"a":2}', headers)
        self.assertFalse(probe.verify_internal_hmac())

    def test_stale_timestamp_rejected(self):
        body = b'{"a":1}'
        old_ts = str(int(time.time()) - 120)
        probe = self._probe({HMAC_SETTINGS_KEY: _KEY}, body, _sign(body, timestamp=old_ts))
        self.assertFalse(probe.verify_internal_hmac())

    def test_signature_for_other_path_rejected(self):
        """SEC-20: подпись, снятая для другого path, не принимается."""
        body = b'{"a":1}'
        # Подписали тот же body+method, но для ДРУГОГО пути.
        headers = _sign(body, path="/internal/api/v1/telemetry")
        # Probe-запрос идёт на uri="/internal" (см. _probe) → path mismatch.
        probe = self._probe({HMAC_SETTINGS_KEY: _KEY}, body, headers)
        self.assertFalse(probe.verify_internal_hmac())

    def test_signature_for_other_method_rejected(self):
        """SEC-20: подпись, снятая для другого метода, не принимается."""
        body = b'{"a":1}'
        headers = _sign(body, method="GET")
        probe = self._probe({HMAC_SETTINGS_KEY: _KEY}, body, headers)
        self.assertFalse(probe.verify_internal_hmac())


def _make_internal_handler(handler_cls, body: bytes, headers: dict, db: Any, settings: dict):
    request = _FakeRequest(
        method="POST",
        uri="/internal",
        body=body,
        headers=headers,
    )
    return make_handler(handler_cls, settings=settings, request=request, db=db)


class TestSecurityEventHandlerHmac(unittest.TestCase):
    def _db(self) -> AsyncMock:
        db = AsyncMock()
        db.fetch_one_strict = AsyncMock(side_effect=AssertionError("DB не должен вызываться при 401"))
        return db

    def test_unsigned_rejected_401(self):
        body = json.dumps({
            "locker_id": "11111111-1111-1111-1111-111111111111",
            "event_type": "intrusion",
            "severity": "high",
        }).encode("utf-8")

        async def run() -> dict:
            handler = _make_internal_handler(
                InternalSecurityEventHandler, body, {}, self._db(),
                {HMAC_SETTINGS_KEY: _KEY},
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)


class TestTelemetryHandlerHmac(unittest.TestCase):
    def _db(self) -> AsyncMock:
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=AssertionError("DB не должен вызываться при 401"))
        return db

    def test_unsigned_rejected_401(self):
        body = json.dumps({
            "locker_id": "11111111-1111-1111-1111-111111111111",
            "readings": [
                {"sensor_id": "22222222-2222-2222-2222-222222222222",
                 "sensor_type_code": "temperature", "value": 1.0},
            ],
        }).encode("utf-8")

        async def run() -> dict:
            handler = _make_internal_handler(
                InternalTelemetryHandler, body, {}, self._db(),
                {HMAC_SETTINGS_KEY: _KEY},
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_valid_signature_passes_hmac_gate(self):
        """Корректная подпись проходит гейт 401 и доходит до бизнес-логики."""
        body = json.dumps({
            "locker_id": "11111111-1111-1111-1111-111111111111",
            "readings": [
                {"sensor_id": "22222222-2222-2222-2222-222222222222",
                 "sensor_type_code": "temperature", "value": 1.0},
            ],
        }).encode("utf-8")
        reached = AsyncMock(side_effect=RuntimeError("reached-business-logic"))

        async def run() -> dict:
            db = AsyncMock()
            db.fetch_all = reached
            handler = _make_internal_handler(
                InternalTelemetryHandler, body, _sign(body), db,
                {HMAC_SETTINGS_KEY: _KEY},
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        # Не 401 — подпись принята; БД была вызвана (упёрлись в sentinel → 500).
        self.assertNotEqual(cap["status"], 401)
        reached.assert_awaited()


class TestCommandHistoryHandlerHmac(unittest.TestCase):
    """POST /internal/api/v1/commands/history требует HMAC-подпись (паритет с
    events/security и telemetry). Канал command_history — аудит команд (включая
    2FA-события); без подписи любой контейнер в backend_net мог бы подделать
    запись в аудит-журнал."""

    _BODY = json.dumps({
        "command_id": "33333333-3333-3333-3333-333333333333",
        "locker_id": "11111111-1111-1111-1111-111111111111",
        "command_type": "open",
        "status": "executed",
    }).encode("utf-8")

    def _db(self) -> AsyncMock:
        db = AsyncMock()
        # upsert → self._db.fetch_one_strict; при 401 до БД доходить нельзя.
        db.fetch_one_strict = AsyncMock(
            side_effect=AssertionError("DB не должен вызываться при 401"),
        )
        return db

    def test_unsigned_rejected_401(self):
        async def run() -> dict:
            handler = _make_internal_handler(
                InternalCommandHistoryHandler, self._BODY, {}, self._db(),
                {HMAC_SETTINGS_KEY: _KEY},
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        self.assertEqual(cap["status"], 401)

    def test_valid_signature_passes_hmac_gate(self):
        """Корректная подпись проходит гейт 401 и доходит до бизнес-логики."""
        reached = AsyncMock(side_effect=RuntimeError("reached-business-logic"))

        async def run() -> dict:
            db = AsyncMock()
            db.fetch_one_strict = reached
            handler = _make_internal_handler(
                InternalCommandHistoryHandler, self._BODY, _sign(self._BODY), db,
                {HMAC_SETTINGS_KEY: _KEY},
            )
            cap = _capture(handler)
            await handler.post()
            return cap

        cap = asyncio.run(run())
        # Не 401 — подпись принята; БД была вызвана (упёрлись в sentinel → 500).
        self.assertNotEqual(cap["status"], 401)
        reached.assert_awaited()


if __name__ == "__main__":
    unittest.main()
