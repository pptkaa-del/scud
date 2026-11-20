"""Tests for COR-33: relay resilience — malformed MQTT frames and fleet isolation.

Defect A: _on_message must survive decode_com raising on malformed payload.
Defect B: run_fleet must isolate per-relay failures (return_exceptions=True).
"""
from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from scud_relay.device import RelayDevice
from scud_relay.mqtt_transport import RelayMqttTransport, _MQTT_ACK_OK
from scud_relay.protocol import encode_rep, Com


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_transport(device_id: int = 1) -> RelayMqttTransport:
    """Build a RelayMqttTransport with a stub gmqtt.Client (no real broker)."""
    dev = RelayDevice(device_id=device_id, version=2)
    transport = RelayMqttTransport.__new__(RelayMqttTransport)
    transport._dev = dev
    transport._host = "localhost"
    transport._port = 1883
    transport._keepalive = 95
    transport._status_interval_s = 300.0
    # Replace the gmqtt client with a stub that records publish calls
    transport._client = MagicMock()
    return transport


def _valid_com_payload() -> bytes:
    """12-byte LOCK_OPEN frame (golden: cmd_id=7, command=5, data=0)."""
    return (7).to_bytes(4, "little") + (5).to_bytes(4, "little") + (0).to_bytes(4, "little")


# ---------------------------------------------------------------------------
# Defect A — _on_message must not raise on malformed payload
# ---------------------------------------------------------------------------

class TestOnMessageMalformedPayload:
    """_on_message with a bad frame must swallow the error and return _MQTT_ACK_OK."""

    async def test_malformed_payload_does_not_raise(self):
        transport = _make_transport(device_id=1)
        fake_client = MagicMock()

        result = await transport._on_message(
            fake_client, "com/1", b"\xff\xff garbage", 1, None
        )

        assert result == _MQTT_ACK_OK

    async def test_malformed_payload_no_publish(self):
        """No rep or stat should be published when the frame is malformed."""
        transport = _make_transport(device_id=1)
        fake_client = MagicMock()

        await transport._on_message(
            fake_client, "com/1", b"\xff\xff garbage", 1, None
        )

        fake_client.publish.assert_not_called()

    async def test_malformed_payload_logs_warning(self, caplog):
        """A warning must be emitted with device_id and payload info."""
        transport = _make_transport(device_id=42)
        fake_client = MagicMock()

        with caplog.at_level(logging.WARNING, logger="scud_relay.mqtt_transport"):
            await transport._on_message(
                fake_client, "com/42", b"\xde\xad\xbe", 1, None
            )

        assert len(caplog.records) >= 1
        warning_text = " ".join(r.getMessage() for r in caplog.records)
        # Must mention the device so operators know which relay had the issue
        assert "42" in warning_text

    async def test_empty_payload_does_not_raise(self):
        transport = _make_transport(device_id=1)
        fake_client = MagicMock()

        result = await transport._on_message(
            fake_client, "com/1", b"", 1, None
        )

        assert result == _MQTT_ACK_OK

    async def test_too_long_payload_does_not_raise(self):
        transport = _make_transport(device_id=1)
        fake_client = MagicMock()

        result = await transport._on_message(
            fake_client, "com/1", b"\x00" * 100, 1, None
        )

        assert result == _MQTT_ACK_OK


# ---------------------------------------------------------------------------
# Defect A positive control — happy path still works after the fix
# ---------------------------------------------------------------------------

class TestOnMessageHappyPath:
    """Well-formed frames must still produce the expected rep/stat publishes."""

    async def test_lock_open_produces_rep_and_stat(self):
        transport = _make_transport(device_id=1)
        fake_client = MagicMock()

        payload = _valid_com_payload()  # cmd_id=7, command=5 (LOCK_OPEN), data=0
        result = await transport._on_message(fake_client, "com/1", payload, 1, None)

        assert result == _MQTT_ACK_OK
        assert fake_client.publish.call_count == 2  # rep + stat

        # First publish is the rep
        rep_call = fake_client.publish.call_args_list[0]
        assert rep_call[0][0] == "rep"
        # rep payload: device_id=1, cmd_id=7, result=1 (SUCCESS)
        assert rep_call[0][1] == encode_rep(1, 7, 1)

        # Second publish is the retained stat
        stat_call = fake_client.publish.call_args_list[1]
        assert stat_call[0][0] == "stat"

    async def test_send_status_publishes_only_stat(self):
        """SEND_STATUS (command=4): stat published, no rep (outcome.rep is False)."""
        dev = RelayDevice(device_id=2, version=2)
        transport = RelayMqttTransport.__new__(RelayMqttTransport)
        transport._dev = dev
        transport._host = "localhost"
        transport._port = 1883
        transport._keepalive = 95
        transport._status_interval_s = 300.0
        transport._client = MagicMock()

        fake_client = MagicMock()
        # cmd_id=1, command=4 (SEND_STATUS), data=0
        payload = (1).to_bytes(4, "little") + (4).to_bytes(4, "little") + (0).to_bytes(4, "little")

        result = await transport._on_message(fake_client, "com/2", payload, 1, None)

        assert result == _MQTT_ACK_OK
        assert fake_client.publish.call_count == 1
        stat_call = fake_client.publish.call_args_list[0]
        assert stat_call[0][0] == "stat"


# ---------------------------------------------------------------------------
# Defect A cancellation safety — CancelledError must propagate, not be swallowed
# ---------------------------------------------------------------------------

class TestOnMessageCancellationSafety:
    """asyncio.CancelledError must not be caught by the resilience wrapper."""

    async def test_cancelled_error_propagates(self, monkeypatch):
        """If apply_command raises CancelledError it must escape _on_message."""
        transport = _make_transport(device_id=1)
        fake_client = MagicMock()

        def _raise_cancelled(com: Com) -> None:
            raise asyncio.CancelledError()

        monkeypatch.setattr(transport._dev, "apply_command", _raise_cancelled)

        # Use a well-formed payload so decode_com succeeds and we reach apply_command
        payload = _valid_com_payload()

        with pytest.raises(asyncio.CancelledError):
            await transport._on_message(fake_client, "com/1", payload, 1, None)


# ---------------------------------------------------------------------------
# Defect B — run_fleet must isolate per-relay failures
# ---------------------------------------------------------------------------

class TestRunFleetIsolation:
    """A single relay's run() raising must not cancel the other relays."""

    async def test_one_failure_does_not_cancel_others(self, monkeypatch):
        """Transport #0 raises immediately; transport #1 runs until cancelled."""
        import scud_relay.fleet as fleet_mod

        started: list[int] = []
        cancel_event = asyncio.Event()

        async def run_raises():
            raise ConnectionRefusedError("broker refused")

        async def run_ok():
            started.append(1)
            # Wait until the gather finishes and we can inspect started
            await cancel_event.wait()

        # Patch RelayMqttTransport so we don't need a real broker
        class _StubTransport:
            def __init__(self, dev: RelayDevice, host: str, port: int,
                         **_kwargs) -> None:
                self._dev = dev
                self._id = dev.device_id

            def run(self):
                if self._id == 1:  # first device (base_id=1 + 0)
                    return run_raises()
                return run_ok()

        monkeypatch.setattr(fleet_mod, "RelayMqttTransport", _StubTransport)

        # Signal run_ok to exit shortly after gather starts
        async def _signal_and_gather():
            # Start the fleet gather but cancel run_ok after a short time
            async def _trigger():
                await asyncio.sleep(0.05)
                cancel_event.set()

            trigger = asyncio.ensure_future(_trigger())
            await fleet_mod.run_fleet(count=2, host="localhost", port=1883, base_id=1)
            trigger.cancel()

        # run_fleet must complete without raising, even though device #1 failed
        await _signal_and_gather()

        # run_ok was entered (device #2 was not cancelled early by device #1's failure)
        assert started == [1], "second relay must have been allowed to run"

    async def test_run_fleet_does_not_propagate_exception(self, monkeypatch):
        """run_fleet must not raise even when all transports fail immediately."""
        import scud_relay.fleet as fleet_mod

        async def run_raises():
            raise ConnectionRefusedError("broker refused")

        class _AlwaysFailTransport:
            def __init__(self, dev: RelayDevice, host: str, port: int,
                         **_kwargs) -> None:
                pass

            def run(self):
                return run_raises()

        monkeypatch.setattr(fleet_mod, "RelayMqttTransport", _AlwaysFailTransport)

        # Must not raise — all failures are absorbed
        await fleet_mod.run_fleet(count=3, host="localhost", port=1883)

    async def test_run_fleet_logs_failures(self, monkeypatch, caplog):
        """Each relay failure must be logged so operators can identify it."""
        import scud_relay.fleet as fleet_mod

        async def run_raises():
            raise ConnectionRefusedError("broker refused relay")

        class _FailTransport:
            def __init__(self, dev: RelayDevice, host: str, port: int,
                         **_kwargs) -> None:
                self._dev = dev

            def run(self):
                return run_raises()

        monkeypatch.setattr(fleet_mod, "RelayMqttTransport", _FailTransport)

        with caplog.at_level(logging.ERROR, logger="scud_relay.fleet"):
            await fleet_mod.run_fleet(count=2, host="localhost", port=1883, base_id=10)

        # Both devices (id 10 and 11) should be logged
        assert len(caplog.records) >= 2


# ---------------------------------------------------------------------------
# Defect B — CancelledError must propagate out of run_fleet, not be swallowed
# ---------------------------------------------------------------------------

class TestRunFleetCancellationPropagation:
    """Regression for review finding I-1: asyncio.CancelledError is a BaseException,
    not an Exception, so the original isinstance(result, Exception) check silently
    swallowed cancellations instead of propagating them."""

    async def test_cancelled_error_propagates_out_of_run_fleet(self, monkeypatch):
        """A CancelledError from one transport must be re-raised by run_fleet,
        not absorbed as a regular per-device failure.

        This test would FAIL on the pre-fix code where the gather loop only
        checked isinstance(result, Exception) — CancelledError would be silently
        dropped and run_fleet would return None instead of raising.
        """
        import scud_relay.fleet as fleet_mod

        async def run_cancelled():
            raise asyncio.CancelledError()

        class _CancelledTransport:
            def __init__(self, dev: RelayDevice, host: str, port: int,
                         **_kwargs) -> None:
                pass

            def run(self):
                return run_cancelled()

        monkeypatch.setattr(fleet_mod, "RelayMqttTransport", _CancelledTransport)

        with pytest.raises(asyncio.CancelledError):
            await fleet_mod.run_fleet(count=1, host="localhost", port=1883)

    async def test_plain_exception_still_logged_not_raised(self, monkeypatch, caplog):
        """A plain Exception (e.g. ConnectionRefusedError) must still be
        logged-and-isolated after the fix — the fix must not break isolation."""
        import scud_relay.fleet as fleet_mod

        async def run_raises():
            raise ConnectionRefusedError("broker down")

        class _FailTransport:
            def __init__(self, dev: RelayDevice, host: str, port: int,
                         **_kwargs) -> None:
                self._dev = dev

            def run(self):
                return run_raises()

        monkeypatch.setattr(fleet_mod, "RelayMqttTransport", _FailTransport)

        # Must NOT raise — plain Exception is absorbed
        with caplog.at_level(logging.ERROR, logger="scud_relay.fleet"):
            await fleet_mod.run_fleet(count=1, host="localhost", port=1883, base_id=5)

        assert len(caplog.records) >= 1
