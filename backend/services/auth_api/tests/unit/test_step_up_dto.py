"""Unit-тесты валидации step-up DTO (VAL-02)."""
from __future__ import annotations

import unittest

from app.handlers.dto.step_up_dto import VerifyStepUpDTO

_VALID_UUID = "22222222-2222-2222-2222-222222222222"


class TestVerifyStepUpDTO(unittest.TestCase):

    def test_valid_uuid_challenge_id_ok(self) -> None:
        dto = VerifyStepUpDTO(challenge_id=_VALID_UUID, user_id=42, code="123456")
        self.assertEqual(dto.challenge_id, _VALID_UUID)

    def test_non_uuid_challenge_id_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            VerifyStepUpDTO(
                challenge_id="not-a-uuid", user_id=42, code="123456",
            )

    def test_empty_challenge_id_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            VerifyStepUpDTO(challenge_id="", user_id=42, code="123456")


if __name__ == "__main__":
    unittest.main()
