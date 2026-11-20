import asyncio

from app.mqtt import relay_codec as rc
from app.mqtt.ingest import RelayIngest


def _u32(n: int) -> bytes:
    return n.to_bytes(4, "little")


def _stat_v1(relay_id, relays=(1, 0, 0, 0), status=1):
    return (_u32(relay_id) + _u32(1) + b"".join(_u32(r) for r in relays)
            + _u32(status) + _u32(20) + _u32(0))


def _stat_v2(relay_id, relays=(0, 1, 0, 2), relay_en=1, status=1):
    return (_u32(relay_id) + _u32(2) + _u32(2) + _u32(1) + _u32(80) + _u32(1)
            + _u32(relay_en) + b"".join(_u32(r) for r in relays)
            + _u32(status) + _u32(20))


class FakeRepo:
    def __init__(self):
        self.online = True
        self.board_updates = []

    async def get_locker_id_by_relay_id(self, relay_id):
        return "L5" if relay_id == 5 else None

    async def update_connectivity(self, locker_id, value):
        self.online = True

    async def mark_offline_if_online(self, locker_id):
        was, self.online = self.online, False
        return was

    async def sync_door_state(self, locker_id, door):
        return None

    async def get_operational_status(self, locker_id):
        return None

    async def update_board_info(self, locker_id, board_version, has_relays):
        self.board_updates.append((locker_id, board_version, has_relays))
        return True


class FakeRelays:
    def __init__(self, fail=False):
        self.ensured = []
        self.reported = []
        self.fail = fail

    async def ensure_channels(self, locker_id):
        self.ensured.append(locker_id)

    async def set_reported(self, locker_id, states):
        if self.fail:
            raise RuntimeError("db down")
        self.reported.append((locker_id, dict(states)))


class FakeQueue:
    def __init__(self):
        self.registered = {}

    async def register_connection(self, locker_id, transport):
        self.registered[locker_id] = transport

    async def unregister_connection(self, locker_id, transport=None):
        self.registered.pop(locker_id, None)

    def is_connected(self, locker_id):
        return locker_id in self.registered

    def get_transport(self, locker_id):
        return self.registered.get(locker_id)


def _build(relays=None):
    repo = FakeRepo()
    relays = relays if relays is not None else FakeRelays()
    ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=None,
                         mqtt_client=object(), relay_repo=relays)
    return ingest, repo, relays


def test_v1_stat_marks_relays_present_and_writes_states():
    async def scenario():
        ingest, repo, relays = _build()
        await ingest.handle_message("stat", _stat_v1(5, relays=(1, 0, 2, 0)))
        assert repo.board_updates == [("L5", 1, True)]
        assert relays.ensured == ["L5"]
        assert relays.reported == [("L5", {1: 1, 2: 0, 3: 2, 4: 0})]
    asyncio.run(scenario())


def test_v2_relay_en_1_writes_states():
    async def scenario():
        ingest, repo, relays = _build()
        await ingest.handle_message("stat", _stat_v2(5, relays=(0, 1, 0, 2)))
        assert repo.board_updates == [("L5", 2, True)]
        assert relays.reported == [("L5", {1: 0, 2: 1, 3: 0, 4: 2})]
    asyncio.run(scenario())


def test_v2_relay_en_0_marks_absent_and_skips_states():
    async def scenario():
        ingest, repo, relays = _build()
        await ingest.handle_message("stat", _stat_v2(5, relays=(2, 2, 2, 2), relay_en=0))
        assert repo.board_updates == [("L5", 2, False)]
        assert relays.ensured == []
        assert relays.reported == []
    asyncio.run(scenario())


def test_repeated_heartbeat_writes_only_changes():
    async def scenario():
        ingest, repo, relays = _build()
        await ingest.handle_message("stat", _stat_v2(5, relays=(0, 0, 0, 0)))
        await ingest.handle_message("stat", _stat_v2(5, relays=(0, 0, 0, 0)))
        await ingest.handle_message("stat", _stat_v2(5, relays=(1, 0, 0, 0)))
        assert repo.board_updates == [("L5", 2, True)]
        assert relays.ensured == ["L5"]
        assert relays.reported == [
            ("L5", {1: 0, 2: 0, 3: 0, 4: 0}),
            ("L5", {1: 1, 2: 0, 3: 0, 4: 0}),
        ]
    asyncio.run(scenario())


def test_board_version_change_is_written_again():
    async def scenario():
        ingest, repo, _ = _build()
        await ingest.handle_message("stat", _stat_v2(5, relay_en=1))
        await ingest.handle_message("stat", _stat_v2(5, relay_en=0))
        await ingest.handle_message("stat", _stat_v2(5, relay_en=0))
        assert repo.board_updates == [("L5", 2, True), ("L5", 2, False)]
    asyncio.run(scenario())


def test_out_of_range_state_is_skipped():
    async def scenario():
        ingest, _, relays = _build()
        await ingest.handle_message("stat", _stat_v2(5, relays=(1, 7, 0, 0)))
        assert relays.reported == [("L5", {1: 1, 3: 0, 4: 0})]
    asyncio.run(scenario())


def test_offline_stat_does_not_touch_relays():
    async def scenario():
        ingest, repo, relays = _build()
        await ingest.handle_message("stat", _stat_v2(5, status=0))
        assert repo.board_updates == []
        assert relays.reported == []
    asyncio.run(scenario())


def test_stale_frame_is_skipped():
    async def scenario():
        ingest, _, relays = _build()
        await ingest._sync_relays("L5", rc.decode_stat(_stat_v2(5, relays=(1, 1, 1, 1))), 10)
        await ingest._sync_relays("L5", rc.decode_stat(_stat_v2(5, relays=(0, 0, 0, 0))), 3)
        assert relays.reported == [("L5", {1: 1, 2: 1, 3: 1, 4: 1})]
    asyncio.run(scenario())


def test_relay_db_failure_does_not_break_stat_and_retries():
    async def scenario():
        relays = FakeRelays(fail=True)
        ingest, repo, _ = _build(relays)
        await ingest.handle_message("stat", _stat_v2(5, relays=(1, 0, 0, 0)))
        relays.fail = False
        await ingest.handle_message("stat", _stat_v2(5, relays=(1, 0, 0, 0)))
        assert relays.reported == [("L5", {1: 1, 2: 0, 3: 0, 4: 0})]
        assert repo.online
    asyncio.run(scenario())


def test_without_relay_repo_only_board_info_is_written():
    async def scenario():
        repo = FakeRepo()
        ingest = RelayIngest(repo=repo, command_queue=FakeQueue(), command_results=None,
                             mqtt_client=object())
        await ingest.handle_message("stat", _stat_v1(5))
        assert repo.board_updates == [("L5", 1, True)]
    asyncio.run(scenario())
