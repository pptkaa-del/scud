import asyncio

from app.services.transport import NotificationTransport, RecipientUnreachableError
from shared.enums import NotificationChannel


class _Stub:
    channel = NotificationChannel.SMS

    async def send_text(self, address: str, text: str) -> None:
        return None


def test_stub_satisfies_protocol_at_runtime():
    stub: NotificationTransport = _Stub()
    asyncio.run(stub.send_text("79001234567", "hi"))
    assert stub.channel == NotificationChannel.SMS


def test_recipient_unreachable_carries_address():
    err = RecipientUnreachableError("bad number", address="79001234567")
    assert err.address == "79001234567"
    assert isinstance(err, Exception)
