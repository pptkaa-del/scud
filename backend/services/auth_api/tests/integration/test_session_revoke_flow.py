"""Integration: POST /internal/api/v1/sessions/revoke против live auth_api (SEC-14).

Сидим активные токены пользователя прямо в access_tokens, дёргаем
подписанный endpoint и проверяем, что все токены отозваны и в ответе
вернулся корректный count. Также проверяем, что неподписанный запрос
отклоняется 401, когда auth_api держит ключ.

Запускается в auth_api_tests контейнере: scud_auth поднят миграцией, live
auth_api доступен по TEST_BASE_URL.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import os
import time
import uuid

import psycopg2
import requests

from shared.utils import body_sha256, compute_request_signature

_BASE = os.environ.get("TEST_BASE_URL", "http://auth_api:8003")
_PATH = "/internal/api/v1/sessions/revoke"
_METHOD = "POST"
_KEY = os.environ.get(
    "INTERNAL_SESSION_REVOKE_HMAC_KEY",
    "internal-session-revoke-dev-key-do-not-use-in-prod",
).encode("utf-8")

_N_TOKENS = 4

AUTH_DB = {
    "host": os.environ.get("DB_HOST", "postgres_auth"),
    "port": int(os.environ.get("DB_PORT", 5432)),
    "user": os.environ.get("DB_USER", "admin"),
    "password": os.environ.get("DB_PASSWORD", "password"),
    "dbname": os.environ.get("DB_NAME", "scud_auth"),
}


def _conn():
    return psycopg2.connect(**AUTH_DB)


def _unique_user_id() -> int:
    return 910_000_000 + (uuid.uuid4().int % 50_000_000)


def _seed_tokens(conn, user_id: int, n: int) -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=24)
    with conn.cursor() as cur:
        for _ in range(n):
            cur.execute(
                "INSERT INTO access_tokens (user_id, token_hash, expires_at) "
                "VALUES (%s, %s, %s)",
                (user_id, (uuid.uuid4().hex + uuid.uuid4().hex)[:64], expires_at),
            )
    conn.commit()


def _active_count(conn, user_id: int) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM access_tokens "
            "WHERE user_id = %s AND revoked_at IS NULL",
            (user_id,),
        )
        return int(cur.fetchone()[0])


def _post(payload: dict, *, signed: bool) -> requests.Response:
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if signed:
        ts = str(int(time.time()))
        sig = compute_request_signature(ts, _METHOD, _PATH, body_sha256(body), _KEY)
        headers["X-Internal-Timestamp"] = ts
        headers["X-Internal-Signature"] = sig
    return requests.post(f"{_BASE}{_PATH}", data=body, headers=headers, timeout=5)


def test_signed_revoke_revokes_all_tokens() -> None:
    user_id = _unique_user_id()
    conn = _conn()
    try:
        _seed_tokens(conn, user_id, _N_TOKENS)
        assert _active_count(conn, user_id) == _N_TOKENS

        resp = _post({"user_id": user_id}, signed=True)
        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked"] == _N_TOKENS

        assert _active_count(conn, user_id) == 0
    finally:
        conn.close()


def test_unsigned_revoke_rejected_401() -> None:
    user_id = _unique_user_id()
    conn = _conn()
    try:
        _seed_tokens(conn, user_id, _N_TOKENS)

        resp = _post({"user_id": user_id}, signed=False)
        # Live auth_api держит ключ → неподписанный запрос отклонён, токены живы.
        assert resp.status_code == 401, resp.text
        assert _active_count(conn, user_id) == _N_TOKENS
    finally:
        conn.close()
