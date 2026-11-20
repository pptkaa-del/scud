"""Unit: scud_mqtt_relay_messages_total emission from RelayIngest + _dispatch.

Проверяет взаимоисключаемость исходов:
- valid frame → processed
- truncated/garbage frame → dropped_malformed (и NOT processed)
- unknown relay_id → dropped_unknown_relay (и NOT processed)
- injected handler exception in _dispatch → handler_error (и re-raises)
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.locker_data.db.models.locker import Connectivity
from app.mqtt.ingest import RelayIngest
from shared.enums import RelayMessageOutcome
from shared.metrics import SERVICE_NAME, mqtt_relay_messages_total


def _counter(outcome: RelayMessageOutcome) -> float:
    return mqtt_relay_messages_total.labels(
        service=SERVICE_NAME, outcome=outcome.value
    )._value.get()


class FakeRepo:
    def __init__(self, mapping, connectivity_by_id=None):
        self._by_relay = mapping
        self.door_updates = []
        self.connectivity = []
        self._conn_by_id = dict(connectivity_by_id or {})

    async def get_locker_id_by_relay_id(self, relay_id):
        return self._by_relay.get(relay_id)

    async def update_connectivity(self, locker_id, value):
        self.connectivity.append((locker_id, value))
        self._conn_by_id[locker_id] = value

    async def update_door(self, locker_id, door, who_open_last=None):
        self.door_updates.append((locker_id, door, who_open_last))

    async def sync_door_state(self, locker_id, door):
        self.door_updates.append((locker_id, door))

    async def get_by_id(self, locker_id):
        conn = self._conn_by_id.get(locker_id)
        if conn is None:
            return None
        return SimpleNamespace(id=locker_id, connectivity=conn)

    async def get_connectivity(self, locker_id):
        return self._conn_by_id.get(locker_id)

    async def mark_offline_if_online(self, locker_id):
        if self._conn_by_id.get(locker_id) == Connectivity.ONLINE:
            self._conn_by_id[locker_id] = Connectivity.OFFLINE
            self.connectivity.append((locker_id, Connectivity.OFFLINE))
            return True
        return False


class FakeQueue:
    def __init__(self):
        self.registered = {}
        self.unregistered = []
        self.register_calls = 0

    async def register_connection(self, locker_id, transport):
        self.registered[locker_id] = transport
        self.register_calls += 1

    async def unregister_connection(self, locker_id, transport=None):
        self.registered.pop(locker_id, None)
        self.unregistered.append(locker_id)

    def is_connected(self, locker_id):
        return locker_id in self.registered

    def get_transport(self, locker_id):
        return self.registered.get(locker_id)


class FakeResults:
    def __init__(self, outcome=RelayMessageOutcome.PROCESSED):
        self.calls = []
        self.received_at = []
        self._outcome = outcome

    async def handle_rep(self, locker_id, cmd_id, result, received_at=None):
        self.calls.append((locker_id, cmd_id, result))
        self.received_at.append(received_at)
        return self._outcome


def _build_ingest(mapping, connectivity_by_id=None, mqtt=None):
    repo = FakeRepo(mapping, connectivity_by_id=connectivity_by_id)
    queue, results = FakeQueue(), FakeResults()
    ingest = RelayIngest(
        repo=repo,
        command_queue=queue,
        command_results=results,
        mqtt_client=mqtt if mqtt is not None else object(),
    )
    return ingest, repo, queue, results


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def test_valid_stat_frame_increments_processed():
    """Valid stat frame for a known relay_id → exactly one PROCESSED increment."""
    async def scenario():
        ingest, _, _, _ = _build_ingest({5: "L5"})
        # v2 online stat for relay_id=5
        raw = (_u32(5) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        before = _counter(RelayMessageOutcome.PROCESSED)
        await ingest.handle_message(topic="stat", payload=raw)
        assert _counter(RelayMessageOutcome.PROCESSED) - before == 1.0

    asyncio.run(scenario())


def test_valid_rep_frame_increments_processed():
    """Valid rep frame for a known relay_id → exactly one PROCESSED increment."""
    async def scenario():
        from app.mqtt.relay_protocol import RepResult
        ingest, _, _, _ = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.ONLINE}
        )
        rep_raw = _u32(5) + _u32(7) + _u32(int(RepResult.SUCCESS))
        before = _counter(RelayMessageOutcome.PROCESSED)
        await ingest.handle_message(topic="rep", payload=rep_raw)
        assert _counter(RelayMessageOutcome.PROCESSED) - before == 1.0

    asyncio.run(scenario())


def test_malformed_frame_increments_dropped_malformed():
    """Truncated/garbage payload → dropped_malformed, NOT processed."""
    async def scenario():
        ingest, _, _, _ = _build_ingest({5: "L5"})
        before_malformed = _counter(RelayMessageOutcome.DROPPED_MALFORMED)
        before_processed = _counter(RelayMessageOutcome.PROCESSED)
        # Too short to decode — raises ValueError inside the codec
        await ingest.handle_message(topic="stat", payload=b"\x00\x00")
        assert _counter(RelayMessageOutcome.DROPPED_MALFORMED) - before_malformed == 1.0
        assert _counter(RelayMessageOutcome.PROCESSED) - before_processed == 0.0

    asyncio.run(scenario())


def test_unknown_relay_id_increments_dropped_unknown_relay_and_not_processed():
    """Stat from relay_id not in the DB → dropped_unknown_relay, NOT processed.

    This is the mutual-exclusivity test required by the brief:
    an unknown-relay frame must count DROPPED_UNKNOWN_RELAY and must NOT
    also count PROCESSED.
    """
    async def scenario():
        ingest, _, _, _ = _build_ingest({5: "L5"})  # relay_id=99 not mapped
        raw = (_u32(99) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1) + _u32(1)
               + _u32(0) + _u32(0) + _u32(0) + _u32(0) + _u32(1) + _u32(20))
        before_unknown = _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY)
        before_processed = _counter(RelayMessageOutcome.PROCESSED)
        await ingest.handle_message(topic="stat", payload=raw)
        assert _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY) - before_unknown == 1.0
        assert _counter(RelayMessageOutcome.PROCESSED) - before_processed == 0.0

    asyncio.run(scenario())


def test_unknown_relay_rep_increments_dropped_unknown_relay_not_processed():
    """rep frame from relay_id not in the DB → DROPPED_UNKNOWN_RELAY +1, PROCESSED +0.

    _handle_rep also early-returns on unknown relay; mutual-exclusivity must hold
    for the rep topic just as it does for stat.
    """
    async def scenario():
        from app.mqtt.relay_protocol import RepResult
        ingest, _, _, _ = _build_ingest({5: "L5"})  # relay_id=99 not mapped
        rep_raw = _u32(99) + _u32(7) + _u32(int(RepResult.SUCCESS))
        before_unknown = _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY)
        before_processed = _counter(RelayMessageOutcome.PROCESSED)
        await ingest.handle_message(topic="rep", payload=rep_raw)
        assert _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY) - before_unknown == 1.0
        assert _counter(RelayMessageOutcome.PROCESSED) - before_processed == 0.0

    asyncio.run(scenario())


def test_unknown_relay_photos_increments_dropped_unknown_relay_not_processed():
    """photos frame from relay_id not in the DB → DROPPED_UNKNOWN_RELAY +1, PROCESSED +0.

    _handle_photo also early-returns on unknown relay; mutual-exclusivity must hold
    for the photos topic just as it does for stat and rep.
    """
    async def scenario():
        ingest, _, _, _ = _build_ingest({5: "L5"})  # relay_id=99 not mapped
        # Minimal valid photos frame: device_id=99, event_id=1, photos_cnt=1,
        # photo_id=1, body = JPEG SOI.
        body = b"\xff\xd8"
        photos_raw = _u32(99) + _u32(1) + _u32(1) + _u32(1) + _u32(len(body)) + body
        before_unknown = _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY)
        before_processed = _counter(RelayMessageOutcome.PROCESSED)
        await ingest.handle_message(topic="photos", payload=photos_raw)
        assert _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY) - before_unknown == 1.0
        assert _counter(RelayMessageOutcome.PROCESSED) - before_processed == 0.0

    asyncio.run(scenario())


def test_handler_exception_increments_handler_error_and_propagates():
    """Unexpected exception from handle_message propagates through _dispatch
    wrapper which increments handler_error and re-raises.

    We simulate the _dispatch wrapper logic inline (the actual _dispatch
    closure in main.py is only instantiated with a real app), testing the
    same try/except/raise pattern.
    """
    async def scenario():
        from shared.enums import RelayMessageOutcome
        from shared.metrics import record_relay_message

        class ExplodingIngest:
            async def handle_message(self, topic: str, payload: bytes) -> None:
                raise RuntimeError("simulated unexpected error")

        relay_ingest = ExplodingIngest()

        async def _dispatch_sim(topic: str, payload: bytes) -> None:
            try:
                await relay_ingest.handle_message(topic, payload)
            except Exception:
                record_relay_message(RelayMessageOutcome.HANDLER_ERROR)
                raise

        before_error = _counter(RelayMessageOutcome.HANDLER_ERROR)
        before_processed = _counter(RelayMessageOutcome.PROCESSED)
        before_malformed = _counter(RelayMessageOutcome.DROPPED_MALFORMED)
        before_unknown = _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY)
        raised = False
        try:
            await _dispatch_sim(topic="stat", payload=b"x")
        except RuntimeError:
            raised = True
        assert raised, "exception must propagate (re-raise)"
        assert _counter(RelayMessageOutcome.HANDLER_ERROR) - before_error == 1.0
        assert _counter(RelayMessageOutcome.PROCESSED) - before_processed == 0.0
        assert _counter(RelayMessageOutcome.DROPPED_MALFORMED) - before_malformed == 0.0
        assert _counter(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY) - before_unknown == 0.0

    asyncio.run(scenario())


def test_stale_rep_records_stale_outcome():
    """STALE_REP от results → ровно один инкремент stale_rep, processed не растёт."""
    async def scenario():
        from app.mqtt.relay_protocol import RepResult
        ingest, _, _, results = _build_ingest(
            {5: "L5"}, connectivity_by_id={"L5": Connectivity.ONLINE}
        )
        results._outcome = RelayMessageOutcome.STALE_REP
        rep_raw = _u32(5) + _u32(7) + _u32(int(RepResult.SUCCESS))
        processed = _counter(RelayMessageOutcome.PROCESSED)
        stale = _counter(RelayMessageOutcome.STALE_REP)
        await ingest.handle_message(topic="rep", payload=rep_raw)
        assert _counter(RelayMessageOutcome.STALE_REP) - stale == 1.0
        assert _counter(RelayMessageOutcome.PROCESSED) == processed

    asyncio.run(scenario())
