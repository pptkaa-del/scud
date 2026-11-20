"""E2E: relay-двойник, фото-серия → intrusion security_event + просматриваемые кадры.

Доказывает цепочку «доказательство взлома» на релейном MQTT-транспорте:
  1. RelayRunner.publish_photo() публикует серию из 5 кадров в topic ``photos``,
     каждый кадр — отдельный JPEG.
  2. locker_api: первый кадр серии порождает intrusion security_event (порог и
     StateMonitor в этой цепочке не участвуют), каждый кадр — отдельное медиа.
  3. MediaProducer пишет кадры в outbound_history_events → outbound_dispatcher
     доставляет multipart в history_api /internal/api/v1/media.
  4. history_api привязывает кадр к событию по security_event_uid, в каком бы
     порядке ни доехали событие и кадры.
  5. Оператор: GET /api/v1/events/security/{id}/media → кадры по photo_id,
     GET /api/v1/media/{id} → один JPEG + Content-Type.
"""

from __future__ import annotations

import hashlib
import logging
import time

import docker

from utils.api_client import GatewayClient
from utils.mqtt_locker import _create_mqtt_locker, _random_relay_id
from utils.relay_runner import RelayRunner
from utils.wait import wait_for

logger = logging.getLogger(__name__)

_FRAMES = 5
_REDELIVERY_SETTLE_S = 15
_JPEG_SOI_APP = b"\xff\xd8\xff"
_LOCKER_DB_CONTAINER = "backend-postgres_locker-1"


def _intrusions(gateway: GatewayClient, locker_id: str) -> list[dict]:
    return [
        e for e in gateway.get_security_events(locker_id=locker_id, event_type="intrusion")
        if e.get("event_type") == "intrusion" and e.get("locker_id") == locker_id
    ]


def _intrusion_outbox_rows(locker_id: str) -> int:
    """Сколько intrusion-строк locker_api поставил в outbox для шкафа.

    history_api дедуплицирует событие по event_uid, поэтому лишняя постановка
    видна только в outbox locker_api.
    """
    result = docker.from_env().containers.get(_LOCKER_DB_CONTAINER).exec_run([
        "psql", "-U", "admin", "-d", "scud_lockers", "-tAc",
        "SELECT count(*) FROM outbound_history_events "
        "WHERE channel = 'security_event' AND payload->>'event_type' = 'intrusion' "
        f"AND locker_id = '{locker_id}'",
    ])
    assert result.exit_code == 0, result.output
    return int(result.output.decode().strip())


def _wait_incident_with_frames(gateway: GatewayClient, locker_id: str) -> tuple[dict, list[dict]]:
    wait_for(
        lambda: bool(_intrusions(gateway, locker_id)),
        timeout=90, interval=3,
        message=f"intrusion-событие по фото-серии для {locker_id} не появилось",
    )
    incident = _intrusions(gateway, locker_id)[-1]
    wait_for(
        lambda: len(gateway.get_event_media(incident["id"])) >= _FRAMES,
        timeout=60, interval=3,
        message=f"не все {_FRAMES} кадров привязались к событию {incident['id']}",
    )
    return incident, gateway.get_event_media(incident["id"])


class TestTamperPhotoSeries:
    def test_tamper_photo_series_creates_intrusion_with_one_media_per_frame(
        self, gateway: GatewayClient
    ):
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            with RelayRunner(relay_id) as runner:
                runner.publish_stat()
                runner.publish_photo(count=_FRAMES)

                incident, media = _wait_incident_with_frames(gateway, locker_id)
                assert incident["severity"] == "high"
                assert len(media) == _FRAMES, media
                assert [m["metadata"]["photo_id"] for m in media] == [1, 2, 3, 4, 5]
                assert all(m["kind"] == "photo" for m in media)
                assert all(m["mime_type"] == "image/jpeg" for m in media)

                digests = set()
                for item in media:
                    photo_bytes, photo_ct = gateway.get_media_bytes(item["id"])
                    assert photo_ct.startswith("image/jpeg"), photo_ct
                    assert photo_bytes[:3] == _JPEG_SOI_APP, photo_bytes[:4].hex()
                    assert photo_bytes.count(_JPEG_SOI_APP) == 1, (
                        f"media {item['id']} looks like several concatenated JPEGs"
                    )
                    digests.add(hashlib.sha256(photo_bytes).hexdigest())
                assert len(digests) == _FRAMES
                assert _intrusion_outbox_rows(locker_id) == 1

                logger.info("tamper photo-series OK: event_id=%d media=%s",
                            incident["id"], [m["id"] for m in media])
        finally:
            gateway.delete_locker(locker_id)

    def test_redelivered_frame_does_not_duplicate_media_or_incident(
        self, gateway: GatewayClient
    ):
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            with RelayRunner(relay_id) as runner:
                runner.publish_stat()
                runner.publish_photo(count=_FRAMES)
                incident, media = _wait_incident_with_frames(gateway, locker_id)
                series_uid = media[0]["metadata"]["series_uid"]

                runner.republish_photo_frame(3)
                # Отрицательная проверка: даём кадру время пройти outbox и history_api.
                time.sleep(_REDELIVERY_SETTLE_S)

                media_after = gateway.get_event_media(incident["id"])
                assert len(media_after) == _FRAMES, media_after
                same_series = [
                    e for e in _intrusions(gateway, locker_id)
                    if (e.get("metadata") or {}).get("series_uid") == series_uid
                ]
                assert len(same_series) == 1, same_series
        finally:
            gateway.delete_locker(locker_id)
