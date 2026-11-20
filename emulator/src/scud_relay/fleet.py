"""Spawn N headless relay devices against a broker (mirrors scud fleet style)."""
from __future__ import annotations

import asyncio
import logging
import ssl

from .device import RelayDevice
from .mqtt_transport import DEFAULT_PHOTOS_PER_INCIDENT, RelayMqttTransport

logger = logging.getLogger(__name__)


async def run_fleet(count: int, host: str, port: int, base_id: int = 1,
                    version: int = 2, ramp_up_seconds: float = 0.0,
                    events: bool = False, events_interval_s: float = 30.0,
                    photos: bool = False,
                    tls_context: ssl.SSLContext | None = None,
                    per_id_topics: bool = False,
                    photos_per_incident: int = DEFAULT_PHOTOS_PER_INCIDENT) -> None:
    devices = [RelayDevice(device_id=base_id + i, version=version) for i in range(count)]
    transports = [
        RelayMqttTransport(d, host, port, events=events,
                           events_interval_s=events_interval_s, photos=photos,
                           tls_context=tls_context,
                           per_id_topics=per_id_topics,
                           photos_per_incident=photos_per_incident)
        for d in devices
    ]
    # Ramp-up: разносим подключения во времени. Без него N плат коннектятся и
    # шлют connect-stat одновременно (thundering herd → ingest DB-executor wedge).
    # step=0 → прежнее поведение (всё сразу) при ramp_up_seconds=0.
    step = (ramp_up_seconds / count) if (ramp_up_seconds > 0 and count > 0) else 0.0

    async def _staggered(idx: int, transport: RelayMqttTransport) -> None:
        if step:
            await asyncio.sleep(idx * step)
        await transport.run()

    results = await asyncio.gather(
        *(_staggered(i, transports[i]) for i in range(count)), return_exceptions=True)
    for device, result in zip(devices, results):
        if isinstance(result, (asyncio.CancelledError, KeyboardInterrupt)):
            raise result
        if isinstance(result, Exception):
            logger.error("relay device %s terminated with error: %s", device.device_id, result)
