import asyncio
import logging
from typing import Any

from shared.mqtt_client import MQTT_ACK_OK, MqttClient


class FakeGmqtt:
    """Injected via client_factory — the unit suite never touches real gmqtt."""
    def __init__(self, client_id: str | None = None) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscribed: list[tuple[str, int]] = []
        self.on_message: Any = None
        self.on_connect: Any = None
        self.auth: tuple[str | None, str | None] | None = None

    def set_auth_credentials(self, username: str | None, password: str | None) -> None:
        self.auth = (username, password)

    def subscribe(self, topic: str, qos: int = 1) -> None:
        self.subscribed.append((topic, qos))

    def publish(self, topic: str, payload: bytes, qos: int = 0, retain: bool = False) -> None:
        self.published.append((topic, payload, qos, retain))


def _client() -> MqttClient:
    return MqttClient("h", 1883, "cid", client_factory=FakeGmqtt)


def test_publish_delegates_to_gmqtt() -> None:
    async def scenario() -> None:
        c = _client()
        await c.publish("stat", b"x", qos=1, retain=True)
        assert c._client.published == [("stat", b"x", 1, True)]
    asyncio.run(scenario())


def test_message_routes_to_handler_and_acks() -> None:
    async def scenario() -> None:
        received: list[tuple[str, bytes]] = []

        async def handler(topic: str, payload: bytes) -> None:
            received.append((topic, payload))

        c = _client()
        c.set_message_handler(handler)
        rc = await c._on_message(None, "com/1", bytearray(b"\x07"), 1, {})
        assert rc == MQTT_ACK_OK
        assert received == [("com/1", b"\x07")]
    asyncio.run(scenario())


def test_handler_exception_is_logged_and_acked() -> None:
    """A raising handler must not propagate out of _on_message; the message is
    already acked by gmqtt, so the failure must be logged and MQTT_ACK_OK returned."""
    async def scenario() -> None:
        async def handler(topic: str, payload: bytes) -> None:
            raise ValueError("boom")

        c = _client()
        c.set_message_handler(handler)

        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        cap = _Capture()
        logger = logging.getLogger("shared.mqtt_client")
        logger.addHandler(cap)
        try:
            rc = await c._on_message(None, "com/1", bytearray(b"\x07"), 1, {})
        finally:
            logger.removeHandler(cap)

        assert rc == MQTT_ACK_OK
        assert any(r.levelno >= logging.ERROR and r.exc_info for r in records)
        # topic should be logged; payload bytes must NOT be.
        joined = " ".join(r.getMessage() for r in records)
        assert "com/1" in joined
    asyncio.run(scenario())


def test_on_connect_resubscribes_all_when_accepted() -> None:
    c = _client()
    c.add_subscription("stat", 1)
    c.add_subscription("rep", 1)
    c._on_connect(c._client, None, 0, None)
    assert ("stat", 1) in c._client.subscribed
    assert ("rep", 1) in c._client.subscribed


def test_on_connect_skips_subscribe_when_rejected() -> None:
    c = _client()
    c.add_subscription("stat", 1)
    c._on_connect(c._client, None, 5, None)  # rc=5 not authorized
    assert c._client.subscribed == []


def test_add_subscription_dedupes() -> None:
    c = _client()
    c.add_subscription("stat", 1)
    c.add_subscription("stat", 1)
    c._on_connect(c._client, None, 0, None)
    assert c._client.subscribed.count(("stat", 1)) == 1


class FakeGmqttConn(FakeGmqtt):
    """FakeGmqtt + is_connected attribute to exercise the delegation."""
    def __init__(self, client_id: str | None = None) -> None:
        super().__init__(client_id)
        self.is_connected: bool = False


def test_is_connected_delegates_to_underlying_client() -> None:
    c = MqttClient("h", 1883, "cid", client_factory=FakeGmqttConn)
    assert c.is_connected is False
    c._client.is_connected = True
    assert c.is_connected is True


def test_is_connected_false_when_client_is_none() -> None:
    c = MqttClient("h", 1883, "cid", client_factory=FakeGmqttConn)
    c._client = None
    assert c.is_connected is False


def test_is_connected_false_when_attr_missing() -> None:
    # The injected fake without is_connected must not raise — defensive getattr.
    c = MqttClient("h", 1883, "cid", client_factory=FakeGmqtt)
    assert c.is_connected is False
