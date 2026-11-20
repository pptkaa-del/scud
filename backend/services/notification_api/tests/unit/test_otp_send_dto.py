import pytest

from app.handlers.dto.otp_send_dto import SendOtpDTO


def test_address_optional_defaults_none():
    dto = SendOtpDTO(user_id=1, code="123456")
    assert dto.address is None


def test_address_accepted():
    dto = SendOtpDTO(user_id=1, code="123456", address="+79001234567")
    assert dto.address == "+79001234567"


def test_address_too_long_rejected():
    with pytest.raises(ValueError):
        SendOtpDTO(user_id=1, code="123456", address="x" * 65)
