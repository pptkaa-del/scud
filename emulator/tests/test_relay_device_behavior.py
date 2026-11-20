import hashlib

from scud_relay.device import RelayDevice
from scud_relay.mqtt_transport import broker_password, broker_username
from scud_relay.protocol import Com


def test_lock_open_sets_door_and_returns_success():
    dev = RelayDevice(device_id=1, version=2)
    result = dev.apply_command(Com(cmd_id=10, command=5, data=0))  # LOCK_OPEN
    assert result.result == 1  # SUCCESS
    assert dev.door == 1  # OPEN
    assert result.publish_stat is True  # state change -> stat published


def test_lock_open_on_v1_not_supported():
    dev = RelayDevice(device_id=1, version=1)
    result = dev.apply_command(Com(cmd_id=11, command=5, data=0))
    assert result.result == 3  # NOT_SUPPORTED


def test_send_status_publishes_stat_without_rep():
    dev = RelayDevice(device_id=1, version=2)
    result = dev.apply_command(Com(cmd_id=0, command=4, data=0))  # SEND_STATUS
    assert result.rep is False  # no rep
    assert result.publish_stat is True


def test_relay_on_when_relay_disabled_not_supported():
    dev = RelayDevice(device_id=1, version=2, relay_en=0)
    result = dev.apply_command(Com(cmd_id=12, command=1, data=1))  # RELAY_ON relay#1
    assert result.result == 3  # NOT_SUPPORTED, no state change
    assert dev.relays[0] == 0


def test_relay_reboot_cycles_relay():
    dev = RelayDevice(device_id=1, version=2, relay_en=1)
    dev.relays = [1, 0, 0, 0]
    result = dev.apply_command(Com(cmd_id=13, command=3, data=1))  # REBOOT relay#1
    assert result.result == 1  # SUCCESS


def test_cmd_none_is_noop_without_rep_or_stat():
    dev = RelayDevice(device_id=1, version=2)
    before = list(dev.relays)
    result = dev.apply_command(Com(cmd_id=99, command=0, data=0))  # CMD_NONE
    assert result.rep is False  # no rep published
    assert result.publish_stat is False  # no stat published
    assert dev.relays == before  # no state change


def test_relay_command_invalid_number_not_supported():
    dev = RelayDevice(device_id=1, version=2, relay_en=1)
    before = list(dev.relays)
    result = dev.apply_command(Com(cmd_id=20, command=1, data=5))  # RELAY_ON relay#5
    assert result.result == 3  # NOT_SUPPORTED
    assert result.rep is True
    assert result.publish_stat is False
    assert dev.relays == before  # no state change


def test_relay_on_all_relays():
    dev = RelayDevice(device_id=1, version=2, relay_en=1)
    dev.relays = [0, 0, 0, 0]
    result = dev.apply_command(Com(cmd_id=21, command=1, data=0))  # RELAY_ON all
    assert result.result == 1  # SUCCESS
    assert dev.relays == [1, 1, 1, 1]


def test_locked_relay_returns_failure_and_keeps_state():
    dev = RelayDevice(device_id=1, version=2, relay_en=1, locked_relays={1})
    dev.relays = [0, 0, 0, 0]
    result = dev.apply_command(Com(cmd_id=22, command=1, data=1))  # RELAY_ON relay#1
    assert result.result == 2  # FAILURE
    assert dev.relays[0] == 0  # state unchanged (locked)


def test_lock_open_on_locked_door_returns_failure():
    dev = RelayDevice(device_id=1, version=2, door_locked=True)
    assert dev.door == 2  # CLOSED
    result = dev.apply_command(Com(cmd_id=23, command=5, data=0))  # LOCK_OPEN
    assert result.result == 2  # FAILURE
    assert dev.door == 2  # CLOSED unchanged (locked)


def test_broker_cred_derivation_matches_protocol():
    assert broker_username(12345) == "relay-12345"
    assert broker_password(1) == hashlib.sha256(b"relay-1").digest()[:16].hex()
