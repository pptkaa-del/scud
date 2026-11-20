"""Fleet provisioning: deterministic addresses, POST payload shape, 409 handling.

Pure logic + a fake session — no gateway, no network.
"""
from __future__ import annotations

import pytest

from scud_relay.provisioning import (
    _PLACES,
    GatewaySession,
    build_locker_payload,
    generate_location,
    provision_fleet,
)


class TestGenerateLocation:
    def test_deterministic(self):
        assert generate_location(7) == generate_location(7)

    def test_distinct_addresses_across_indices(self):
        addrs = {generate_location(i).address for i in range(24)}
        assert len(addrs) == 24  # no collisions across two full wraps

    def test_address_and_coords_shape(self):
        loc = generate_location(0)
        assert "д. " in loc.address
        assert "," in loc.address
        assert -90 <= loc.lat <= 90
        assert -180 <= loc.lon <= 180

    def test_building_number_increments_after_wrap(self):
        first = generate_location(0)
        wrapped = generate_location(len(_PLACES))  # same street, next building
        assert first.address.endswith("д. 1")
        assert wrapped.address.endswith("д. 2")

    def test_negative_index_rejected(self):
        with pytest.raises(ValueError):
            generate_location(-1)


class TestBuildPayload:
    def test_payload_keys(self):
        payload = build_locker_payload(relay_id=5001, index=3)
        assert payload["transport"] == "mqtt"
        assert payload["relay_id"] == 5001
        assert payload["inventory_number"] == "RELAY-5001"
        assert payload["location_address"] == generate_location(3).address
        assert isinstance(payload["location_lat"], float)
        assert isinstance(payload["location_lon"], float)


class _FakeSession:
    """Records create_locker payloads and returns a scripted status sequence."""

    def __init__(self, statuses: list[int]) -> None:
        self._statuses = statuses
        self.payloads: list[dict] = []
        self._n = 0

    def create_locker(self, payload: dict) -> tuple[int, dict]:
        self.payloads.append(payload)
        status = self._statuses[self._n] if self._n < len(self._statuses) else 201
        self._n += 1
        body = {"id": f"uuid-{payload['relay_id']}"} if status == 201 else {}
        return status, body


class TestProvisionFleet:
    def test_creates_count_lockers_with_sequential_relay_ids(self):
        sess = _FakeSession([201, 201, 201])
        outcomes = provision_fleet(sess, count=3, base_id=5001)
        assert [o.relay_id for o in outcomes] == [5001, 5002, 5003]
        assert all(o.created for o in outcomes)
        assert [o.locker_id for o in outcomes] == ["uuid-5001", "uuid-5002", "uuid-5003"]
        assert [p["relay_id"] for p in sess.payloads] == [5001, 5002, 5003]

    def test_409_is_idempotent_skip(self):
        sess = _FakeSession([201, 409, 201])
        outcomes = provision_fleet(sess, count=3, base_id=1)
        assert [o.created for o in outcomes] == [True, False, True]
        assert outcomes[1].status == 409

    def test_401_aborts(self):
        with pytest.raises(RuntimeError):
            provision_fleet(_FakeSession([401]), count=2, base_id=1)

    def test_403_aborts(self):
        with pytest.raises(RuntimeError):
            provision_fleet(_FakeSession([403]), count=2, base_id=1)

    def test_index_start_offsets_addresses(self):
        sess = _FakeSession([201])
        provision_fleet(sess, count=1, base_id=1, index_start=5)
        assert sess.payloads[0]["location_address"] == generate_location(5).address


class _FakeHeaders:
    def __init__(self, cookies: list[str]) -> None:
        self._cookies = cookies

    def get_all(self, _name: str) -> list[str]:
        return self._cookies


class _FakeResp:
    def __init__(self, cookies: list[str]) -> None:
        self.headers = _FakeHeaders(cookies)


class TestGatewaySession:
    def test_set_access_token(self):
        sess = GatewaySession("http://gw")
        sess.set_access_token("abc123")
        assert sess._cookies["access_token"] == "abc123"

    def test_store_cookies_parses_setcookie(self):
        sess = GatewaySession("http://gw")
        sess._store_cookies(_FakeResp(
            ["access_token=xyz; HttpOnly; Path=/", "other=1; Path=/"]))
        assert sess._cookies["access_token"] == "xyz"
        assert sess._cookies["other"] == "1"

    def test_base_url_trailing_slash_stripped(self):
        assert GatewaySession("http://gw/").login  # constructs without error
        assert GatewaySession("http://gw/")._base == "http://gw"
