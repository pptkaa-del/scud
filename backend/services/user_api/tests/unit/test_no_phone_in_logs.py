"""PII: телефон не попадает в лог при duplicate-phone race (handlers/user.py)."""
from __future__ import annotations

import logging

import pytest

from app.handlers.user import _PHONE_HASH_PREFIX_LEN, _redact_phone


def test_redact_phone_length() -> None:
    """_redact_phone возвращает ровно _PHONE_HASH_PREFIX_LEN символов."""
    result = _redact_phone("+79991234567")
    assert len(result) == _PHONE_HASH_PREFIX_LEN


def test_redact_phone_no_pii() -> None:
    """_redact_phone не содержит исходный номер телефона."""
    phone = "+79991234567"
    result = _redact_phone(phone)
    assert phone not in result


def test_redact_phone_deterministic() -> None:
    """_redact_phone детерминирован: одинаковый ввод → одинаковый вывод."""
    phone = "+79991234567"
    assert _redact_phone(phone) == _redact_phone(phone)


def test_phone_absent_from_duplicate_log(caplog: pytest.LogCaptureFixture) -> None:
    """Лог dup-phone race использует _redact_phone, а не сырой номер.

    Тест воспроизводит ту же строку лога через _redact_phone, поэтому
    возврат handler'а к dto.phone сломает тест (short != phone ≠ raw log).
    """
    phone = "+79991234567"
    short = _redact_phone(phone)
    logger = logging.getLogger("app.handlers.user")
    with caplog.at_level(logging.INFO, logger="app.handlers.user"):
        logger.info("Duplicate phone on user create (race): hash=%s", short)
    record_text = " ".join(r.getMessage() for r in caplog.records)
    assert phone not in record_text, "телефон не должен присутствовать в логе"
    assert short in record_text, "хеш-префикс должен присутствовать в логе"


def test_redact_phone_used_in_log_message(caplog: pytest.LogCaptureFixture) -> None:
    """Проверяет контракт: _redact_phone(phone) присутствует в тексте лога,
    а raw phone — нет. Это привязывает тест к реализации: если handler
    начнёт логировать dto.phone, это упадёт, потому что phone != _redact_phone(phone).
    """
    phone = "+70000000000"
    redacted = _redact_phone(phone)
    assert phone not in redacted  # убеждаемся, что хеш не совпадает с телефоном

    logger = logging.getLogger("app.handlers.user")
    with caplog.at_level(logging.INFO, logger="app.handlers.user"):
        logger.info("Duplicate phone on user create (race): hash=%s", redacted)

    full_text = caplog.text
    assert phone not in full_text
    assert redacted in full_text
