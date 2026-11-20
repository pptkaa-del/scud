"""Integration: POST /commands и /commands/confirm для relay-типов против реальной БД."""
from __future__ import annotations

from collections.abc import Iterator
import os
import re
import time
from typing import Any
import uuid

import psycopg2
import pytest
import requests

BASE_URL = os.getenv("TEST_BASE_URL", "http://locker_api:8001")
PROBLEM = "https://api.scuddocs.com/problems/"


def _locker_db() -> Any:
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "postgres_locker"),
        port=int(os.getenv("DB_PORT", "5432")),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud_lockers"),
    )


def _user_db() -> Any:
    return psycopg2.connect(
        host=os.getenv("USER_DB_HOST", "postgres_user"),
        port=int(os.getenv("USER_DB_PORT", "5432")),
        user="admin",
        password=os.getenv("DB_PASSWORD", "password"),
        dbname="scud_users",
    )


def _notification_db() -> Any:
    return psycopg2.connect(
        host=os.getenv("NOTIFICATION_DB_HOST", "postgres_notification"),
        port=int(os.getenv("NOTIFICATION_DB_PORT", "5432")),
        user="admin",
        password=os.getenv("DB_PASSWORD", "password"),
        dbname="scud_notifications",
    )


def _exec(sql: str, params: tuple[Any, ...]) -> list[tuple[Any, ...]]:
    conn = _locker_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall() if cur.description else []
        conn.commit()
    finally:
        conn.close()
    return rows


def _set_relay(locker_id: str, channel: int, **fields: Any) -> None:
    assignments = ", ".join(f"{name} = %s" for name in fields)
    _exec(
        f"UPDATE locker_relays SET {assignments} WHERE locker_id = %s AND channel = %s",
        (*fields.values(), locker_id, channel),
    )


@pytest.fixture
def relay_locker() -> Iterator[str]:
    inv = f"INT-RELAY-{uuid.uuid4().hex[:8]}"
    rows = _exec(
        """
        INSERT INTO lockers (inventory_number, state, connectivity, last_seen_at,
                             has_relays, board_version)
        VALUES (%s, 'active', 'online', now(), TRUE, 2)
        RETURNING id
        """,
        (inv,),
    )
    locker_id = str(rows[0][0])
    _exec(
        "INSERT INTO locker_relays (locker_id, channel) SELECT %s, c FROM generate_series(1, 4) AS c",
        (locker_id,),
    )
    yield locker_id
    _exec("DELETE FROM locker_commands WHERE locker_id = %s", (locker_id,))
    _exec("DELETE FROM lockers WHERE id = %s", (locker_id,))


def _post(locker_id: str, body: dict[str, Any], *, role: str = "operator",
          user_id: int = 1, require_2fa: bool = False) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands",
        headers={
            "X-Remote-User": str(user_id),
            "X-Remote-Role": role,
            "X-Remote-Lockers": "*",
            "X-Remote-Require-2FA": "true" if require_2fa else "false",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=10,
    )


def _assert_problem(resp: requests.Response, status: int, code: str) -> None:
    assert resp.status_code == status, resp.text
    assert resp.json()["type"] == PROBLEM + code


def _relay_commands(locker_id: str) -> list[tuple[Any, ...]]:
    return _exec(
        "SELECT type::text, parameters, status::text FROM locker_commands "
        "WHERE locker_id = %s ORDER BY created_at",
        (locker_id,),
    )


def test_operator_relay_on_created(relay_locker: str) -> None:
    resp = _post(relay_locker, {"type": "relay_on", "parameters": {"relay": 2}})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["type"] == "relay_on"
    assert body["parameters"] == {"relay": 2}
    assert _relay_commands(relay_locker) == [("relay_on", {"relay": 2}, "pending")]


def test_second_active_command_on_channel_conflicts(relay_locker: str) -> None:
    assert _post(relay_locker, {"type": "relay_on", "parameters": {"relay": 1}}).status_code == 201
    _assert_problem(
        _post(relay_locker, {"type": "relay_off", "parameters": {"relay": 1}}),
        409, "relay-command-in-progress",
    )
    assert _post(relay_locker, {"type": "relay_off", "parameters": {"relay": 2}}).status_code == 201
    _exec("UPDATE locker_commands SET status = 'executed' WHERE locker_id = %s", (relay_locker,))
    assert _post(relay_locker, {"type": "relay_restart", "parameters": {"relay": 1}}).status_code == 201


def test_invalid_parameters_422(relay_locker: str) -> None:
    for parameters in ({}, {"relay": 5}, {"relay": True}, {"relay": "2"}, {"relay": 1, "x": 1}):
        _assert_problem(
            _post(relay_locker, {"type": "relay_on", "parameters": parameters}),
            422, "relay-invalid-parameters",
        )
    assert _relay_commands(relay_locker) == []


def test_worker_forbidden(relay_locker: str) -> None:
    resp = _post(relay_locker, {"type": "relay_on", "parameters": {"relay": 1}}, role="worker")
    assert resp.status_code == 403, resp.text


def test_block_absent_409(relay_locker: str) -> None:
    _exec("UPDATE lockers SET has_relays = FALSE WHERE id = %s", (relay_locker,))
    _assert_problem(
        _post(relay_locker, {"type": "relay_on", "parameters": {"relay": 1}}),
        409, "relay-block-absent",
    )


def test_board_offline_409(relay_locker: str) -> None:
    _exec("UPDATE lockers SET connectivity = 'offline' WHERE id = %s", (relay_locker,))
    _assert_problem(
        _post(relay_locker, {"type": "relay_on", "parameters": {"relay": 1}}),
        409, "board-offline",
    )


def test_channel_unavailable_409(relay_locker: str) -> None:
    _set_relay(relay_locker, 1, control_enabled=False)
    _set_relay(relay_locker, 2, is_active=False)
    _exec("DELETE FROM locker_relays WHERE locker_id = %s AND channel = 3", (relay_locker,))
    for channel in (1, 2, 3):
        _assert_problem(
            _post(relay_locker, {"type": "relay_on", "parameters": {"relay": channel}}),
            409, "relay-channel-unavailable",
        )


def test_protected_channel_only_administrator(relay_locker: str) -> None:
    _set_relay(relay_locker, 4, is_protected=True)
    _assert_problem(
        _post(relay_locker, {"type": "relay_off", "parameters": {"relay": 4}}),
        403, "relay-channel-protected",
    )
    resp = _post(
        relay_locker, {"type": "relay_off", "parameters": {"relay": 4}}, role="administrator",
    )
    assert resp.status_code == 201, resp.text


@pytest.fixture
def operator_with_2fa() -> Iterator[int]:
    try:
        conn = _user_db()
    except psycopg2.OperationalError:
        pytest.skip("user_db недоступен — нужен полный compose-стек")
    phone = f"+7901{uuid.uuid4().int % 10**7:07d}"
    max_user_id = 910_000_000 + uuid.uuid4().int % 10**7
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (full_name, phone, role, state, max_user_id) "
                "VALUES ('Relay Operator INT', %s, 'operator', 'active', %s) RETURNING id",
                (phone, max_user_id),
            )
            user_id = int(cur.fetchone()[0])
            cur.execute(
                "INSERT INTO user_auth (user_id, password_hash, require_2fa) VALUES (%s, %s, TRUE)",
                (user_id, "x" * 64),
            )
        conn.commit()
    finally:
        conn.close()
    yield user_id
    conn = _user_db()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM user_auth WHERE user_id = %s", (user_id,))
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
        conn.commit()
    finally:
        conn.close()


def _peek_code(user_id: int) -> str | None:
    for _ in range(10):
        try:
            conn = _notification_db()
        except psycopg2.OperationalError:
            return None
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT body FROM outbound_max_messages WHERE user_id = %s "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        match = re.search(r"\b(\d{6})\b", row[0]) if row else None
        if match:
            return match.group(1)
        time.sleep(1)
    return None


def _challenge(locker_id: str, user_id: int, channel: int) -> tuple[str, str]:
    resp = _post(
        locker_id, {"type": "relay_off", "parameters": {"relay": channel}},
        user_id=user_id, require_2fa=True,
    )
    if resp.status_code != 202:
        pytest.skip(f"auth_api не выдал challenge (HTTP {resp.status_code}): {resp.text}")
    assert _relay_commands(locker_id) == []
    code = _peek_code(user_id)
    if code is None:
        pytest.skip("OTP-код не появился в outbox — notification_api недоступен?")
    return resp.json()["challenge_id"], code


def _confirm(locker_id: str, user_id: int, challenge_id: str, code: str) -> requests.Response:
    return requests.post(
        f"{BASE_URL}/api/v1/locker/{locker_id}/commands/confirm",
        headers={
            "X-Remote-User": str(user_id),
            "X-Remote-Role": "operator",
            "X-Remote-Lockers": "*",
            "Content-Type": "application/json",
        },
        json={"challenge_id": challenge_id, "code": code},
        timeout=10,
    )


def test_relay_2fa_challenge_not_issued_when_denied(relay_locker: str, operator_with_2fa: int) -> None:
    _set_relay(relay_locker, 2, is_protected=True)
    _assert_problem(
        _post(relay_locker, {"type": "relay_off", "parameters": {"relay": 2}},
              user_id=operator_with_2fa, require_2fa=True),
        403, "relay-channel-protected",
    )


def test_relay_2fa_confirm_creates_command(relay_locker: str, operator_with_2fa: int) -> None:
    challenge_id, code = _challenge(relay_locker, operator_with_2fa, 3)
    resp = _confirm(relay_locker, operator_with_2fa, challenge_id, code)
    assert resp.status_code == 201, resp.text
    assert _relay_commands(relay_locker) == [("relay_off", {"relay": 3}, "pending")]


def test_relay_2fa_confirm_rechecks_policy(relay_locker: str, operator_with_2fa: int) -> None:
    challenge_id, code = _challenge(relay_locker, operator_with_2fa, 3)
    _set_relay(relay_locker, 3, control_enabled=False)
    _assert_problem(
        _confirm(relay_locker, operator_with_2fa, challenge_id, code),
        409, "relay-channel-unavailable",
    )
    assert _relay_commands(relay_locker) == []
