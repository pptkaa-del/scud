"""OTP-код: генерация и хэширование (единый источник для auth_api).

Логин (login.py), step-up command-2FA (step_up.py) и проверка кода
(verify.py) использовали локальные копии ``_generate_otp`` / ``_hash_otp``.
Дедуплицировано сюда, чтобы алгоритм хэша (SHA-256, hex) и формат кода
(6 цифр, криптостойкий ``secrets.randbelow``) были в одном месте.

Хэш сравнивается через ``hmac.compare_digest`` на стороне вызывающего
(constant-time, SEC-19) — здесь только детерминированный hex-дайджест.
"""
from __future__ import annotations

import hashlib
import secrets

#: Длина OTP-кода в десятичных знаках.
OTP_LENGTH = 6

# Диапазон 6-значного кода: [100000, 999999]. randbelow(900000) ∈ [0, 899999],
# смещение +100000 гарантирует ведущую значащую цифру (без потери энтропии на
# кодах вида 000123).
_OTP_LOWER_BOUND = 10 ** (OTP_LENGTH - 1)
_OTP_RANGE = 9 * _OTP_LOWER_BOUND


def generate_otp() -> str:
    """Криптографически стойкий 6-значный OTP-код (строка)."""
    return str(secrets.randbelow(_OTP_RANGE) + _OTP_LOWER_BOUND)


def hash_otp(code: str) -> str:
    """SHA-256 хэш OTP-кода (hex-строка)."""
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
