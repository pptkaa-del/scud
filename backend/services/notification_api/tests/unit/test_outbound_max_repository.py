"""Unit-тесты SQL репозитория outbound_max_messages.

Проверяют, что mark_sent / mark_failed физически стирают `body` (plaintext
OTP), и что purge_expired удаляет просроченные/терминальные записи (SEC-18).
Используют fake-db, который захватывает (query, params) каждого execute,
чтобы ассертить именно SQL — мок репозитория этого не ловит.
"""
from __future__ import annotations

import asyncio

from app.notification_data.db.repositories.outbound_max import (
    TERMINAL_RETENTION_SECONDS,
    OutboundMaxRepository,
)


class _FakeDB:
    """Захватывает все execute(query, params)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def execute(self, query: str, params: object = None) -> None:
        self.calls.append((query, params))


def _norm(sql: str) -> str:
    return " ".join(sql.split()).lower()


def test_mark_sent_clears_body() -> None:
    db = _FakeDB()
    repo = OutboundMaxRepository(db)  # type: ignore[arg-type]

    asyncio.run(repo.mark_sent(42))

    assert len(db.calls) == 1
    query, params = db.calls[0]
    norm = _norm(query)
    assert "update outbound_max_messages" in norm
    assert "body = null" in norm, f"mark_sent must null out body, got: {norm}"
    assert params == (42,)


def test_mark_failed_clears_body() -> None:
    db = _FakeDB()
    repo = OutboundMaxRepository(db)  # type: ignore[arg-type]

    asyncio.run(repo.mark_failed(7, "boom"))

    assert len(db.calls) == 1
    query, params = db.calls[0]
    norm = _norm(query)
    assert "update outbound_max_messages" in norm
    assert "body = null" in norm, f"mark_failed must null out body, got: {norm}"
    assert params == ("boom", 7)


def test_increment_attempt_keeps_body() -> None:
    """increment_attempt — НЕ терминал (повтор позднее), body должен остаться."""
    db = _FakeDB()
    repo = OutboundMaxRepository(db)  # type: ignore[arg-type]

    asyncio.run(repo.increment_attempt(7, "retry"))

    query, _ = db.calls[0]
    assert "body" not in _norm(query)


def test_insert_includes_channel_and_address() -> None:
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from app.notification_data.db.repositories.outbound_max import OutboundMaxRepository

    db = MagicMock()
    db.fetch_one_strict = AsyncMock(return_value={"id": 7})
    repo = OutboundMaxRepository(db)
    mid = asyncio.run(repo.insert(
        user_id=1, body="Kod", purpose="otp_login", ttl_seconds=300,
        channel="sms", address="79001234567", max_user_id=None,
    ))
    assert mid == 7
    sql = db.fetch_one_strict.call_args.args[0].lower()
    assert "channel" in sql
    assert "address" in sql


def test_purge_expired_sql_scope() -> None:
    db = _FakeDB()
    repo = OutboundMaxRepository(db)  # type: ignore[arg-type]

    asyncio.run(repo.purge_expired())

    assert len(db.calls) == 1
    query, params = db.calls[0]
    norm = _norm(query)
    assert norm.startswith("delete from outbound_max_messages")
    # Просроченные удаляем безусловно (OTP бесполезен после expiry).
    assert "expires_at < now()" in norm
    # Терминальные (sent/failed) старше окна ретенции — тоже.
    assert "sent_at is not null" in norm or "failed_at is not null" in norm
    assert params == (TERMINAL_RETENTION_SECONDS,)
