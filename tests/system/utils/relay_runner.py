"""Background runner for a headless ``scud_relay`` MQTT device double.

The system-test harness is synchronous (``requests``-based pytest), but the
relay transport (:class:`scud_relay.mqtt_transport.RelayMqttTransport`) is async
(vendored gmqtt). :class:`RelayRunner` bridges the two: it spins up a dedicated
asyncio event loop in a background thread, runs the device's ``.run()`` coroutine
there, and exposes the underlying :class:`scud_relay.device.RelayDevice` so tests
can assert on-device state (e.g. door open/closed).

Usage::

    with RelayRunner(relay_id=42) as runner:
        # device is connecting to the broker as relay-42 in the background
        wait_for(lambda: gateway.get_connectivity(locker_id) == "online", ...)
        ...
        assert runner.device.door == Door.OPEN

``__enter__`` starts the thread + connects; ``__exit__`` cancels the run
coroutine and joins the thread cleanly.

Broker host/port come from ``MQTT_BROKER_HOST`` / ``MQTT_BROKER_PORT`` (set in
docker-compose.yaml to ``host.docker.internal:1883``).
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from types import TracebackType

# Imported from submodules: scud_relay/__init__.py is intentionally empty, so
# RelayDevice / RelayMqttTransport are NOT re-exported at the package root.
from scud_relay.device import RelayDevice
from scud_relay.mqtt_transport import (
    DEFAULT_PHOTOS_PER_INCIDENT,
    RelayMqttTransport,
    build_tamper_photo_series,
)

logger = logging.getLogger(__name__)

_DEFAULT_HOST = os.getenv("MQTT_BROKER_HOST", "host.docker.internal")
_DEFAULT_PORT = int(os.getenv("MQTT_BROKER_PORT", "1883"))


class RelayRunner:
    """Run a ``scud_relay`` device in a background asyncio loop/thread.

    Args:
        relay_id: integer relay/board id; device authenticates as ``relay-{id}``.
        version: firmware version (2 supports LOCK_OPEN/LOCK_CLOSE → door state).
        host: broker host (defaults to ``MQTT_BROKER_HOST`` env).
        port: broker port (defaults to ``MQTT_BROKER_PORT`` env).
        connect_timeout: seconds to wait for the gmqtt connect to complete.
        battery: optional battery level published in the v2 stat (telemetry).
        signal: optional signal level published in the stat (telemetry).
        status_interval_s: if set, the device republishes a retained stat every
            ``status_interval_s`` seconds (firmware ``statusTimer`` heartbeat).
            A LOW value keeps the device's ``last_seen_at`` fresh so it stays
            online; stop the runner to let the backend offline-monitor fire.
            ``None`` (default) → no heartbeat (a single retained stat on connect).
    """

    def __init__(
        self,
        relay_id: int,
        *,
        version: int = 2,
        host: str | None = None,
        port: int | None = None,
        connect_timeout: float = 30.0,
        battery: int | None = None,
        signal: int | None = None,
        status_interval_s: float | None = None,
    ) -> None:
        self.relay_id = relay_id
        self.device = RelayDevice(device_id=relay_id, version=version)
        if battery is not None:
            self.device.battery = battery
        if signal is not None:
            self.device.signal = signal
        self._host = host or _DEFAULT_HOST
        self._port = port if port is not None else _DEFAULT_PORT
        self._connect_timeout = connect_timeout
        self._status_interval_s = status_interval_s

        self._transport = RelayMqttTransport(self.device, self._host, self._port)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._connect_error: BaseException | None = None
        self._last_photo_frames: list[bytes] = []

    # ── lifecycle ────────────────────────────────────────────────────────────

    def __enter__(self) -> "RelayRunner":
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"relay-{self.relay_id}",
            daemon=True,
        )
        self._thread.start()
        if not self._connected.wait(timeout=self._connect_timeout):
            self._shutdown()
            raise TimeoutError(
                f"relay-{self.relay_id} did not connect to broker "
                f"{self._host}:{self._port} within {self._connect_timeout}s"
            )
        if self._connect_error is not None:
            self._shutdown()
            raise RuntimeError(
                f"relay-{self.relay_id} failed to connect to broker: "
                f"{self._connect_error!r}"
            ) from self._connect_error
        logger.info(
            "RelayRunner: relay-%s connected to broker %s:%s",
            self.relay_id, self._host, self._port,
        )
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._shutdown()

    # ── on-demand stat ───────────────────────────────────────────────────────

    def publish_stat(self) -> None:
        """Publish a one-off retained ``stat`` with the device's CURRENT state.

        For tests that mutate device state mid-run (e.g. ``device.status = 0``
        to announce offline) and need the backend to ingest it now, rather than
        waiting for the optional heartbeat or a disconnect. Thread-safe:
        schedules the publish on the background loop and blocks until gmqtt has
        accepted it. The device stays connected — only the published state
        changes (mirrors firmware sending a stat with the offline flag set).
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError(f"relay-{self.relay_id} loop is not running")

        async def _do_publish() -> None:
            # Read self._transport._client fresh: a connect-retry may have
            # rebuilt the client, and build_stat() must reflect the latest state.
            self._transport._client.publish(  # noqa: SLF001 - own the client
                "stat", self.device.build_stat(), qos=1, retain=True,
            )

        asyncio.run_coroutine_threadsafe(_do_publish(), loop).result(timeout=10)

    def publish_photo(self, count: int = DEFAULT_PHOTOS_PER_INCIDENT) -> tuple[int, int]:
        """Опубликовать фото-серию из ``count`` кадров; вернуть ``(event_id, count)``.

        Backend порождает intrusion-событие по первому кадру серии, поэтому
        порог и StateMonitor здесь не участвуют. Кадры запоминаются для
        :meth:`republish_photo_frame`.
        """
        event_id = self._transport._next_photo_event_id()  # noqa: SLF001 - own the transport
        frames = build_tamper_photo_series(self.device.device_id, event_id, count)
        self._publish_photo_frames(frames)
        self._last_photo_frames = frames
        return event_id, count

    def republish_photo_frame(self, photo_id: int) -> None:
        """Повторно отправить кадр последней серии байт-в-байт (редоставка QoS1)."""
        self._publish_photo_frames([self._last_photo_frames[photo_id - 1]])

    def _publish_photo_frames(self, frames: list[bytes]) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            raise RuntimeError(f"relay-{self.relay_id} loop is not running")

        async def _do_publish() -> None:
            for frame in frames:
                self._transport._client.publish(  # noqa: SLF001 - own the client
                    "photos", frame, qos=1, retain=False,
                )

        asyncio.run_coroutine_threadsafe(_do_publish(), loop).result(timeout=30)

    # ── internals ────────────────────────────────────────────────────────────

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            self._run_task = loop.create_task(self._run())
            loop.run_until_complete(self._run_task)
        except asyncio.CancelledError:
            pass
        except BaseException as e:  # noqa: BLE001 - surfaced via _connect_error
            # If we never signalled connect, record the error so __enter__ can
            # raise it instead of hanging the test on a silent thread death.
            if not self._connected.is_set():
                self._connect_error = e
                self._connected.set()
            else:
                logger.exception("RelayRunner relay-%s crashed", self.relay_id)
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:  # noqa: BLE001
                pass
            loop.close()

    async def _run(self) -> None:
        # We drive the gmqtt client directly (rather than transport.run()) so we
        # can RETRY the connect: the backend provisions broker creds on locker
        # create, but the broker only reloads them on SIGHUP (~2s later). A
        # device that connects immediately after create races the reload and
        # gets CONNACK 0x87 (Not authorized) — so retry until the reload lands.
        client = self._transport._client  # noqa: SLF001 - own the client lifecycle
        connect_err: BaseException | None = None
        deadline = asyncio.get_event_loop().time() + self._connect_timeout
        while asyncio.get_event_loop().time() < deadline:
            # Disable gmqtt auto-reconnect on every (re)built client: the test
            # owns the lifecycle and a pending reconnect() coroutine would leak
            # ("Task was destroyed but pending") when we tear down the loop.
            client.set_config({"reconnect_retries": 0})
            try:
                await client.connect(
                    self._host, self._port, keepalive=self._transport._keepalive,  # noqa: SLF001
                )
                connect_err = None
                break
            except Exception as e:  # noqa: BLE001 - retry auth/conn races
                connect_err = e
                logger.info(
                    "relay-%s connect attempt failed (%s); retrying after broker "
                    "reload window", self.relay_id, e,
                )
                # gmqtt leaves a half-open connection after a rejected CONNACK;
                # rebuild a fresh client for the next attempt.
                self._rebuild_client()
                client = self._transport._client  # noqa: SLF001
                await asyncio.sleep(2.0)
        if connect_err is not None:
            raise connect_err
        self._connected.set()
        # Optional heartbeat: republish a retained stat every status_interval_s so
        # the backend offline-monitor keeps last_seen_at fresh (device stays
        # online). Stopping the runner cancels this loop → device goes silent.
        if self._status_interval_s is not None:
            self._heartbeat_task = asyncio.ensure_future(self._heartbeat(client))
        # Stay alive until cancelled on shutdown, then disconnect cleanly.
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            if self._heartbeat_task is not None:
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass
            raise

    async def _heartbeat(self, client: object) -> None:
        """Republish a retained stat on a fixed interval (firmware statusTimer)."""
        assert self._status_interval_s is not None
        try:
            while True:
                await asyncio.sleep(self._status_interval_s)
                client.publish(  # type: ignore[attr-defined]
                    "stat", self.device.build_stat(), qos=1, retain=True,
                )
        except asyncio.CancelledError:
            raise

    def _rebuild_client(self) -> None:
        """Recreate the transport's gmqtt client after a failed connect.

        A rejected CONNACK leaves the client's connection state dirty; gmqtt
        does not cleanly support re-connecting the same Client instance after an
        auth failure, so we construct a fresh transport (same device/host/port),
        which re-registers on_connect/on_message handlers and credentials.
        """
        self._transport = RelayMqttTransport(self.device, self._host, self._port)

    def _shutdown(self) -> None:
        loop = self._loop
        task = self._run_task
        if loop is not None and task is not None and not loop.is_closed():
            loop.call_soon_threadsafe(task.cancel)
        if self._thread is not None:
            self._thread.join(timeout=15)
            if self._thread.is_alive():
                logger.warning(
                    "RelayRunner relay-%s thread did not stop within 15s",
                    self.relay_id,
                )
