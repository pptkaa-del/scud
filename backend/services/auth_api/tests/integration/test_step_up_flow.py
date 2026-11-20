"""Integration: issue/verify round-trip через scud_auth.

Dev-окружение не имеет ни SMS-шлюза (``SMS_GATEWAY_URL`` пуст), ни валидного
Max-аккаунта, поэтому реальная доставка кода привязанному max_user_id упёрлась
бы в 503. Тест вместо этого:

1. Проверяет ветку без max-биндинга (пользователь с require_2fa=True,
   max_user_id=None, но phone есть — users.phone NOT NULL): auth_api теперь
   ВСЕГДА создаёт challenge и зовёт notification_api. Т.к. SMS не настроен,
   notification_api откладывает код в outbox (``queued_until_binding``) —
   то же самое MAX-fallback поведение, что и у LOGIN-ветки.
2. Сам сидит challenge прямо в ``step_up_challenges`` (как это сделал бы
   issue-handler) и прогоняет verify → expected 200, повторный verify → 401
   (already used).

E2E с реальной Max-доставкой и read-back из outbox покрывает Task 7
(см. tests/system/scenarios/test_command_2fa.py).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
import time
import uuid

import psycopg2
import requests

from shared.utils import body_sha256, compute_request_signature

_BASE = os.environ.get("TEST_BASE_URL", "http://auth_api:8003")
# Соответствует значению INTERNAL_STEP_UP_HMAC_KEY, прописанному для
# auth_api в docker-compose. Если переменная не выставлена (compose без
# секрета) — auth_api работает в режиме unsigned и подпись игнорируется.
_KEY = os.environ.get(
    "INTERNAL_STEP_UP_HMAC_KEY",
    "internal-step-up-dev-key-do-not-use-in-prod",
).encode("utf-8")

AUTH_DB = {
    "host": os.environ.get("DB_HOST", "postgres_auth"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "admin"),
    "password": os.environ.get("DB_PASSWORD", "password"),
    "dbname": os.environ.get("DB_NAME", "scud_auth"),
}

# DB конфигурация для history_api (scud_history) — для проверки публикации
# security_events из step_up хендлеров. Hostname `postgres_history` —
# в docker-compose сетке сервиса auth_api_tests.
HISTORY_DB = {
    "host": os.environ.get("HISTORY_DB_HOST", "postgres_history"),
    "port": int(os.environ.get("HISTORY_DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "admin"),
    "password": os.environ.get("DB_PASSWORD", "password"),
    "dbname": "scud_history",
}


def _post_signed(path: str, payload: dict) -> requests.Response:
    body = json.dumps(payload).encode()
    ts = str(int(time.time()))
    # Подпись привязана к method+path (SEC-20); path — ровно то, что auth_api
    # видит как self.request.path.
    sig = compute_request_signature(ts, "POST", path, body_sha256(body), _KEY)
    return requests.post(
        f"{_BASE}{path}",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Internal-Timestamp": ts,
            "X-Internal-Signature": sig,
        },
        timeout=5,
    )


def _insert_challenge(
    *,
    user_id: int,
    code: str,
    ttl_seconds: int = 300,
    scope_payload: dict | None = None,
) -> str:
    """Вставить challenge прямо в scud_auth для verify-теста.

    Зеркалит StepUpChallengeRepository.insert — auth_api при issue-вызове
    делает ровно это, плюс просит notification_api доставить код.
    """
    challenge_id = str(uuid.uuid4())
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    expires_at = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    payload = scope_payload or {
        "locker_id": "11111111-1111-1111-1111-111111111111",
        "command_type": "open",
    }
    conn = psycopg2.connect(**AUTH_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO step_up_challenges
                    (id, user_id, scope_kind, scope_payload, code_hash,
                     max_attempts, expires_at)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s, %s)
                """,
                (challenge_id, user_id, "locker_command",
                 json.dumps(payload), code_hash, 5, expires_at),
            )
        conn.commit()
    finally:
        conn.close()
    return challenge_id


def _cleanup_challenges(user_id: int) -> None:
    conn = psycopg2.connect(**AUTH_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM step_up_challenges WHERE user_id = %s",
                (user_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _active_challenge_count(user_id: int) -> int:
    conn = psycopg2.connect(**AUTH_DB)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM step_up_challenges "
                "WHERE user_id = %s AND used_at IS NULL AND expires_at > now()",
                (user_id,),
            )
            return int(cur.fetchone()[0])
    finally:
        conn.close()


def test_issue_creates_challenge_for_user_without_max_via_sms_fallback(
    seeded_user_with_2fa_no_max,
):
    """require_2fa=True, max_user_id=None, phone присутствует (NOT NULL) →
    больше не хард-гейт: notification_api реально вызывается. Если SMS
    настроен — 201 sent. Если нет (dev) и Max не привязан, код не доставлен
    ни одним каналом (``queued_until_binding``) → 201 с challenge_id: код ждёт
    привязки Max в outbox."""
    user_id = seeded_user_with_2fa_no_max["user_id"]
    try:
        issue = _post_signed("/internal/api/v1/step-up/issue", {
            "user_id": user_id,
            "scope_kind": "locker_command",
            "scope_payload": {
                "locker_id": "11111111-1111-1111-1111-111111111111",
                "locker_inv": "INV-01",
                "command_type": "open",
                "parameters": {},
                "max_retries": 3,
            },
            "ttl_seconds": 300,
        })
        assert issue.status_code == 201, issue.text
        body = issue.json()
        assert body["challenge_id"] is not None
        assert body["expires_at"] is not None
        assert body["delivery_status"] in ("sent", "queued_until_binding")
        assert _active_challenge_count(user_id) == 1
    finally:
        _cleanup_challenges(user_id)


def test_verify_round_trip_against_seeded_challenge(seeded_user_with_2fa_no_max):
    """Seed challenge напрямую в scud_auth → verify happy + replay."""
    user_id = seeded_user_with_2fa_no_max["user_id"]
    code = "654321"
    try:
        challenge_id = _insert_challenge(
            user_id=user_id, code=code,
            scope_payload={
                "locker_id": "22222222-2222-2222-2222-222222222222",
                "command_type": "reboot",
            },
        )
        verify = _post_signed("/internal/api/v1/step-up/verify", {
            "challenge_id": challenge_id,
            "user_id": user_id,
            "code": code,
        })
        assert verify.status_code == 200, verify.text
        payload = verify.json()
        assert payload["scope_kind"] == "locker_command"
        assert payload["scope_payload"]["command_type"] == "reboot"

        # Replay → 401 (already used).
        again = _post_signed("/internal/api/v1/step-up/verify", {
            "challenge_id": challenge_id,
            "user_id": user_id,
            "code": code,
        })
        assert again.status_code == 401
    finally:
        _cleanup_challenges(user_id)


def test_verify_wrong_code_returns_401(seeded_user_with_2fa_no_max):
    user_id = seeded_user_with_2fa_no_max["user_id"]
    try:
        challenge_id = _insert_challenge(user_id=user_id, code="111111")
        bad = _post_signed("/internal/api/v1/step-up/verify", {
            "challenge_id": challenge_id,
            "user_id": user_id,
            "code": "222222",
        })
        assert bad.status_code == 401
    finally:
        _cleanup_challenges(user_id)


def test_verify_wrong_user_returns_403(seeded_user_with_2fa_no_max):
    user_id = seeded_user_with_2fa_no_max["user_id"]
    try:
        challenge_id = _insert_challenge(user_id=user_id, code="333333")
        bad = _post_signed("/internal/api/v1/step-up/verify", {
            "challenge_id": challenge_id,
            "user_id": user_id + 9999,
            "code": "333333",
        })
        assert bad.status_code == 403
    finally:
        _cleanup_challenges(user_id)


def _fetch_security_event(event_type: str, locker_id: str) -> dict | None:
    """Найти последнюю запись security_events по event_type + locker_id.

    Доставка событий в history_api асинхронная (через RetryHTTPClient);
    делаем несколько попыток с короткой паузой.
    """
    for _ in range(15):
        conn = psycopg2.connect(**HISTORY_DB)
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT event_type, severity, locker_id::text, metadata
                    FROM security_events
                    WHERE event_type = %s
                      AND locker_id = %s::uuid
                    ORDER BY occurred_at DESC LIMIT 1
                    """,
                    (event_type, locker_id),
                )
                row = cur.fetchone()
                if row is not None:
                    return {
                        "event_type": row[0],
                        "severity": row[1],
                        "locker_id": row[2],
                        "metadata": row[3],
                    }
        finally:
            conn.close()
        time.sleep(0.4)
    return None


def test_verify_publishes_command_2fa_verified_security_event(
    seeded_user_with_2fa_no_max,
):
    """После успешного verify — в scud_history появляется command_2fa_verified."""
    user_id = seeded_user_with_2fa_no_max["user_id"]
    code = "987654"
    locker_id = str(uuid.uuid4())
    try:
        challenge_id = _insert_challenge(
            user_id=user_id, code=code,
            scope_payload={
                "locker_id": locker_id,
                "command_type": "open",
            },
        )
        verify = _post_signed("/internal/api/v1/step-up/verify", {
            "challenge_id": challenge_id,
            "user_id": user_id,
            "code": code,
        })
        assert verify.status_code == 200, verify.text

        event = _fetch_security_event("command_2fa_verified", locker_id)
        assert event is not None, "command_2fa_verified not published to history"
        assert event["severity"] == "info"
        assert event["locker_id"] == locker_id
        meta = event["metadata"]
        assert meta["user_id"] == user_id
        assert meta["command_type"] == "open"
        assert meta["challenge_id"] == challenge_id
        # OTP-код НЕ должен быть в metadata.
        assert "code" not in meta
        assert code not in json.dumps(meta)
    finally:
        _cleanup_challenges(user_id)


def test_verify_invalid_code_publishes_command_2fa_failed(
    seeded_user_with_2fa_no_max,
):
    """Невалидный код → security_event command_2fa_failed с reason=invalid."""
    user_id = seeded_user_with_2fa_no_max["user_id"]
    locker_id = str(uuid.uuid4())
    try:
        challenge_id = _insert_challenge(
            user_id=user_id, code="246810",
            scope_payload={
                "locker_id": locker_id,
                "command_type": "reboot",
            },
        )
        bad = _post_signed("/internal/api/v1/step-up/verify", {
            "challenge_id": challenge_id,
            "user_id": user_id,
            "code": "100000",
        })
        assert bad.status_code == 401

        event = _fetch_security_event("command_2fa_failed", locker_id)
        assert event is not None, "command_2fa_failed not published to history"
        assert event["severity"] == "warning"
        meta = event["metadata"]
        assert meta["reason"] == "invalid"
        assert meta["challenge_id"] == challenge_id
        # OTP-код НЕ должен быть в metadata.
        assert "code" not in meta
        assert "100000" not in json.dumps(meta)
        assert "246810" not in json.dumps(meta)
    finally:
        _cleanup_challenges(user_id)
