import asyncio

from conftest import MQTT_HOST, MQTT_PASSWORD, MQTT_PORT, MQTT_USER

from shared.mqtt_client import MqttClient


def test_publish_subscribe_roundtrip_through_broker() -> None:
    async def scenario() -> None:
        received: list[tuple[str, bytes]] = []

        async def handler(topic: str, payload: bytes) -> None:
            received.append((topic, payload))

        sub = MqttClient(MQTT_HOST, MQTT_PORT, "shared-test-sub",
                         MQTT_USER, MQTT_PASSWORD, keepalive=30)
        sub.set_message_handler(handler)
        sub.add_subscription("stat", 1)
        pub = MqttClient(MQTT_HOST, MQTT_PORT, "shared-test-pub",
                         MQTT_USER, MQTT_PASSWORD, keepalive=30)
        try:
            await sub.connect()  # on_connect subscribes to "stat"
            await pub.connect()
            await pub.publish("stat", b"\xde\xad", qos=1, retain=False)
            for _ in range(50):
                if ("stat", b"\xde\xad") in received:
                    break
                await asyncio.sleep(0.1)
        finally:
            await sub.disconnect()
            await pub.disconnect()
        # Membership, not [0]: `stat` is a shared retained topic, so the broker may
        # deliver a leftover retained relay stat before our message — order-independent.
        assert ("stat", b"\xde\xad") in received

    asyncio.run(scenario())
