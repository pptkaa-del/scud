"""relay --photos: tamper-photo series emission on error incidents.

Verifies the photo frame layout (matches the backend decode contract), that every
frame of a series is its own distinct JPEG, the per-run-unique event_id generator,
and that the transport publishes the series exactly on event-emitting incidents when
photos are enabled (and never otherwise). Pure logic — no broker, no network.
"""
from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from scud_relay.device import RelayDevice
from scud_relay.mqtt_transport import RelayMqttTransport, build_tamper_photo_series
from scud_relay.scenario import Incident
from scud_relay.tamper_photo import TAMPER_JPEG


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def _transport(*, photos: bool = True, device_id: int = 5001) -> RelayMqttTransport:
    """Build a transport without touching gmqtt (mirrors test_relay_rampup_jitter)."""
    t = RelayMqttTransport.__new__(RelayMqttTransport)
    t._dev = RelayDevice(device_id=device_id, version=2)
    t._client = MagicMock()
    t._photos = photos
    t._photos_per_incident = 5
    t._photo_salt = 0x1234
    t._photo_seq = 0
    return t


def _incident(emits: bool) -> Incident:
    return Incident(label="x", emits_event=emits, mutate=lambda _d: None)


class TestTamperPhoto:
    def test_embedded_jpeg_is_a_real_renderable_image(self):
        # SOI ffd8 … EOI ffd9 — a real baseline JPEG a media viewer can render,
        # not opaque bytes. Kept small on purpose (dispatcher backpressure).
        assert TAMPER_JPEG[:2] == b"\xff\xd8"
        assert TAMPER_JPEG[-2:] == b"\xff\xd9"
        assert 200 < len(TAMPER_JPEG) < 20000

    def test_frames_match_backend_decode_contract(self):
        frames = build_tamper_photo_series(device_id=5001, event_id=42, count=5)
        assert len(frames) == 5
        for photo_id, frame in enumerate(frames, start=1):
            body = frame[20:]
            assert frame[:20] == (_u32(5001) + _u32(42) + _u32(5) + _u32(photo_id)
                                  + _u32(len(body)))
            assert body[:2] == b"\xff\xd8"
            assert body[2:4] == b"\xff\xfe"            # COM segment right after SOI
            assert body.rstrip(b"\x00").endswith(b"\xff\xd9")

    def test_frames_are_distinct_after_canonicalization(self):
        def canon(body: bytes) -> bytes:
            return body[: body.rfind(b"\xff\xd9") + 2]

        frames = build_tamper_photo_series(device_id=5001, event_id=42, count=5)
        digests = {hashlib.sha256(canon(f[20:])).hexdigest() for f in frames}
        assert len(digests) == 5

    def test_default_count_is_five(self):
        assert len(build_tamper_photo_series(device_id=1, event_id=1)) == 5

    @pytest.mark.parametrize("count", [0, 17])
    def test_count_bounds(self, count):
        with pytest.raises(ValueError):
            build_tamper_photo_series(device_id=1, event_id=1, count=count)


class TestPhotoEventId:
    def test_monotonic_unique_and_uint32(self):
        t = _transport()
        ids = [t._next_photo_event_id() for _ in range(1000)]
        assert ids == sorted(ids)             # strictly increasing within a run
        assert len(set(ids)) == len(ids)      # no collisions
        assert all(0 <= i <= 0xFFFFFFFF for i in ids)

    def test_salt_separates_devices(self):
        a, b = _transport(device_id=1), _transport(device_id=2)
        a._photo_salt, b._photo_salt = 0x0001, 0x0002
        assert a._next_photo_event_id() != b._next_photo_event_id()


class TestEmitScenarioStep:
    def test_photo_series_published_on_event_incident(self):
        t = _transport(photos=True)
        t._emit_scenario_step(_incident(emits=True))
        calls = t._client.publish.call_args_list
        assert [c.args[0] for c in calls] == ["stat"] + ["photos"] * 5
        photo_ids = [int.from_bytes(c.args[1][12:16], "little") for c in calls[1:]]
        assert photo_ids == [1, 2, 3, 4, 5]
        assert all(c.kwargs["qos"] == 1 for c in calls[1:])

    def test_no_photo_on_non_event_incident(self):
        t = _transport(photos=True)
        t._emit_scenario_step(_incident(emits=False))
        topics = [c.args[0] for c in t._client.publish.call_args_list]
        assert topics == ["stat"]

    def test_no_photo_when_photos_disabled(self):
        t = _transport(photos=False)
        t._emit_scenario_step(_incident(emits=True))
        topics = [c.args[0] for c in t._client.publish.call_args_list]
        assert topics == ["stat"]

    def test_photos_flag_gated_on_events_in_constructor(self):
        """photos=True but events=False → no scenario, no photo emission (self._photos
        collapses to False so a bare --photos never silently spams nothing)."""
        dev = RelayDevice(device_id=1, version=2)
        t = RelayMqttTransport(dev, "localhost", events=False, photos=True)
        assert t._photos is False
        assert t._scenario is None
