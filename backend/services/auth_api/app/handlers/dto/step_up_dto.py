"""DTO для step-up handlers."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
import uuid


@dataclass
class IssueStepUpDTO:
    user_id: int
    scope_kind: str
    scope_payload: dict[str, Any] = field(default_factory=dict)
    ttl_seconds: int = 300

    def __post_init__(self) -> None:
        if not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if not isinstance(self.scope_kind, str) or not self.scope_kind:
            raise ValueError("scope_kind must be non-empty string")
        if len(self.scope_kind) > 64:
            raise ValueError("scope_kind must be at most 64 characters")
        if not isinstance(self.scope_payload, dict):
            raise ValueError("scope_payload must be a JSON object")
        if not isinstance(self.ttl_seconds, int) or self.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        if self.ttl_seconds > 3600:
            raise ValueError("ttl_seconds must be at most 3600")


@dataclass
class VerifyStepUpDTO:
    challenge_id: str
    user_id: int
    code: str

    def __post_init__(self) -> None:
        if not isinstance(self.challenge_id, str) or not self.challenge_id:
            raise ValueError("challenge_id must be non-empty string")
        # challenge_id используется ниже как UUID в SQL-запросе; не-UUID
        # значение должно быть отвергнуто здесь, а не падать DB-ошибкой.
        # Сообщение намеренно общее — не палим, что именно не так.
        try:
            uuid.UUID(self.challenge_id)
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("challenge_id is invalid") from exc
        if not isinstance(self.user_id, int) or self.user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        if not isinstance(self.code, str) or not self.code:
            raise ValueError("code must be non-empty string")
        if len(self.code) > 16:
            raise ValueError("code must be at most 16 characters")
