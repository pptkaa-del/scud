"""Модуль валидации входных данных"""
from __future__ import annotations

import re

# Паттерн для российского номера телефона: +7 и 10 цифр
PHONE_PATTERN = re.compile(r'^\+7\d{10}$')


def validate_phone(phone: str) -> bool:
    """
    Проверяет, что номер телефона соответствует формату +79999999999

    Args:
        phone: Номер телефона для проверки

    Returns:
        True если номер валиден, False в противном случае

    Examples:
        >>> validate_phone("+79001234567")
        True
        >>> validate_phone("79001234567")
        False
        >>> validate_phone("+7900123456")
        False
    """
    if not phone:
        return False
    return PHONE_PATTERN.match(phone) is not None
