"""Контракт транспорта доставки исходящих сообщений (OTP и пр.).

Транспорт-нейтральная дихотомия ошибок:
- ``RecipientUnreachableError`` (permanent) → caller делает ``mark_failed``;
- ``shared.http.UpstreamUnavailableError`` (transient) → ``increment_attempt``.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from shared.enums import NotificationChannel


class RecipientUnreachableError(Exception):
    """Постоянный сбой доставки конкретному получателю (не ретраить)."""

    def __init__(self, message: str, *, address: str) -> None:
        super().__init__(message)
        self.address = address


@runtime_checkable
class NotificationTransport(Protocol):
    channel: NotificationChannel

    async def send_text(self, address: str, text: str) -> None: ...
