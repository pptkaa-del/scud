"""Публикация в per-id топики (ПИВ v2) вместо общих."""
from unittest.mock import MagicMock

from scud_relay.device import RelayDevice
from scud_relay.mqtt_transport import RelayMqttTransport


def _transport(per_id: bool) -> RelayMqttTransport:
    t = RelayMqttTransport(RelayDevice(device_id=77, version=2), "broker",
                           per_id_topics=per_id)
    t._client = MagicMock()
    return t


def _published_topics(t: RelayMqttTransport) -> list[str]:
    return [c.args[0] for c in t._client.publish.call_args_list]


class TestSharedByDefault:
    def test_stat_topic_has_no_suffix(self):
        t = _transport(per_id=False)
        t._on_connect(t._client, None, 0, None)
        assert "stat" in _published_topics(t)

    def test_subscribes_to_own_command_topic(self):
        t = _transport(per_id=False)
        t._on_connect(t._client, None, 0, None)
        t._client.subscribe.assert_called_once_with("com/77", qos=1)


class TestPerId:
    def test_stat_topic_carries_device_id(self):
        t = _transport(per_id=True)
        t._on_connect(t._client, None, 0, None)
        assert "stat/77" in _published_topics(t)
        assert "stat" not in _published_topics(t)

    def test_command_topic_is_unchanged(self):
        """com/{id} и так per-id в обеих схемах."""
        t = _transport(per_id=True)
        t._on_connect(t._client, None, 0, None)
        t._client.subscribe.assert_called_once_with("com/77", qos=1)

    def test_photo_topic_carries_device_id(self):
        from scud_relay.scenario import Incident

        t = _transport(per_id=True)
        t._photos = True
        t._emit_scenario_step(Incident(label="err", emits_event=True, mutate=lambda d: None))
        assert _published_topics(t) == ["stat/77"] + ["photos/77"] * t._photos_per_incident
