"""Интеграционные тесты для internal-эндпойнтов history_api.

Эти эндпойнты доступны без X-Remote-User (закрыты снаружи через Apache),
вызываются продьюсерами locker_api.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
import json as _json
import os
import time

import requests

from shared.utils import body_sha256, compute_request_signature

from .conftest import (
    generate_unique_command_id,
    generate_unique_locker_id,
    generate_unique_sensor_id,
)

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8002")

# Тот же ключ, что у history_api (compose env). Если задан — подписываем
# internal-запросы; иначе history_api работает fail-open и подпись не нужна.
_HMAC_KEY = os.getenv("INTERNAL_HISTORY_HMAC_KEY", "") or None

# 1x1 прозрачный PNG (67 байт) — валидный для PIL.Image.open
_MIN_PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def _sign_headers(method: str, path: str, body: bytes) -> dict[str, str]:
    """X-Internal-* подпись над method+path+raw-body (SEC-05/SEC-20).

    Пусто если ключ не задан. ``path`` — компонент пути без query (ровно то,
    что history_api видит как ``self.request.path``).
    """
    if _HMAC_KEY is None:
        return {}
    ts = str(int(time.time()))
    sig = compute_request_signature(
        timestamp=ts,
        method=method,
        path=path,
        body_sha256_hex=body_sha256(body),
        key=_HMAC_KEY.encode("utf-8"),
    )
    return {"X-Internal-Timestamp": ts, "X-Internal-Signature": sig}


def _post_no_auth(path: str, json_body: dict | None = None, **kwargs) -> requests.Response:
    """POST без X-Remote-User, но с HMAC-подписью (SEC-05).

    Подписывается ровно тот body, что уходит в сеть — сериализуем JSON сами,
    чтобы sha256 совпал на стороне history_api.
    """
    if json_body is not None:
        body = _json.dumps(json_body).encode("utf-8")
        headers = {"Content-Type": "application/json", **_sign_headers("POST", path, body)}
        return requests.post(f"{BASE_URL}{path}", data=body, headers=headers, **kwargs)
    return requests.post(f"{BASE_URL}{path}", **kwargs)


def _post_multipart_signed(path: str, files, data) -> requests.Response:
    """Multipart POST с HMAC-подписью над финальным raw-body (SEC-05).

    requests строит multipart-тело с собственным boundary внутри prepare();
    чтобы подпись sha256(body) совпала с тем, что увидит history_api, готовим
    PreparedRequest и подписываем уже собранное тело.
    """
    req = requests.Request("POST", f"{BASE_URL}{path}", files=files, data=data)
    prepared = req.prepare()
    raw = prepared.body
    raw_bytes = raw if isinstance(raw, bytes) else (raw or "").encode("utf-8")
    for k, v in _sign_headers("POST", path, raw_bytes).items():
        prepared.headers[k] = v
    with requests.Session() as session:
        return session.send(prepared)


# =============================================================================
# /internal/api/v1/telemetry
# =============================================================================


def test_internal_telemetry_accepts_batch_without_auth(api_client):
    """POST на internal-эндпойнт без X-Remote-User → 201, и записи доступны через публичный GET."""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    payload = {
        "readings": [
            {
                "locker_id": locker_id,
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "value": 23.5,
            },
            {
                "locker_id": locker_id,
                "sensor_id": sensor_id,
                "sensor_type_code": "temperature",
                "value": 24.1,
            },
        ]
    }
    resp = _post_no_auth("/internal/api/v1/telemetry", payload)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["inserted"] == 2

    # Чтение через публичный API (с auth) видит то же
    list_resp = api_client.get(f"/api/v1/telemetry/list?locker_id={locker_id}")
    assert list_resp.status_code == 200
    data = list_resp.json()
    assert data["total"] >= 2


def test_internal_telemetry_root_locker_id_propagates(api_client):
    """`locker_id` на верхнем уровне применяется ко всем записям без него."""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()

    payload = {
        "locker_id": locker_id,
        "readings": [
            {"sensor_id": sensor_id, "sensor_type_code": "humidity", "value": 55.0},
            {"sensor_id": sensor_id, "sensor_type_code": "humidity", "value": 56.0},
        ],
    }
    resp = _post_no_auth("/internal/api/v1/telemetry", payload)
    assert resp.status_code == 201

    list_resp = api_client.get(f"/api/v1/telemetry/list?locker_id={locker_id}")
    assert list_resp.json()["total"] >= 2


def test_internal_telemetry_rejects_empty_batch():
    resp = _post_no_auth("/internal/api/v1/telemetry", {"readings": []})
    assert resp.status_code == 400
    assert "non-empty" in resp.json()["detail"].lower()


def test_internal_telemetry_rejects_missing_required_fields():
    locker_id = generate_unique_locker_id()
    # отсутствует sensor_type_code
    resp = _post_no_auth("/internal/api/v1/telemetry", {
        "locker_id": locker_id,
        "readings": [{"sensor_id": generate_unique_sensor_id(), "value": 1.0}],
    })
    assert resp.status_code == 400


# =============================================================================
# /internal/api/v1/commands/history
# =============================================================================


def test_internal_command_history_upserts_without_auth(api_client):
    """Internal command-history POST идемпотентен по command_id без auth."""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()

    base_payload = {
        "command_id": command_id,
        "locker_id": locker_id,
        "locker_inventory_number": "СО №100500",
        "command_type": "open",
        "parameters": {"timeout": 30},
        "created_by": 7,
        "user_phone": "+79990001122",
        "user_full_name": "Иванов И.И.",
    }

    # 1. pending
    r1 = _post_no_auth("/internal/api/v1/commands/history",
                       {**base_payload, "status": "pending"})
    assert r1.status_code == 200, r1.text
    pending = r1.json()
    assert pending["status"] == "pending"
    assert pending["sent_at"] is None
    record_id = pending["id"]

    # 2. sent
    r2 = _post_no_auth("/internal/api/v1/commands/history",
                       {**base_payload, "status": "sent"})
    assert r2.status_code == 200
    sent = r2.json()
    assert sent["id"] == record_id
    assert sent["status"] == "sent"
    assert sent["sent_at"] is not None
    sent_at_first = sent["sent_at"]

    # 3. executed: sent_at не перезаписан, executed_at проставлен
    r3 = _post_no_auth("/internal/api/v1/commands/history",
                       {**base_payload, "status": "executed",
                        "result": {"door": "opened"}})
    assert r3.status_code == 200
    executed = r3.json()
    assert executed["id"] == record_id
    assert executed["status"] == "executed"
    assert executed["sent_at"] == sent_at_first
    assert executed["executed_at"] is not None
    assert executed["result"] == {"door": "opened"}

    # 4. Видно через публичный GET (с auth)
    list_resp = api_client.get(f"/api/v1/commands/history?locker_id={locker_id}")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert any(i["command_id"] == command_id and i["status"] == "executed" for i in items)


def test_internal_command_history_failed_path():
    """status=failed → failed_at проставлен, error_message сохранён."""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()

    payload = {
        "command_id": command_id,
        "locker_id": locker_id,
        "command_type": "open",
        "status": "failed",
        "error_message": "Device unreachable",
    }
    resp = _post_no_auth("/internal/api/v1/commands/history", payload)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "failed"
    assert body["failed_at"] is not None
    assert body["error_message"] == "Device unreachable"


def test_internal_command_history_rejects_invalid_status():
    payload = {
        "command_id": generate_unique_command_id(),
        "locker_id": generate_unique_locker_id(),
        "command_type": "open",
        "status": "garbage",
    }
    resp = _post_no_auth("/internal/api/v1/commands/history", payload)
    assert resp.status_code == 400


# =============================================================================
# /internal/api/v1/media
# =============================================================================


def test_internal_media_uploads_without_auth(api_client):
    """Multipart-аплоад без X-Remote-User → 201, медиа доступно через публичный GET."""
    locker_id = generate_unique_locker_id()

    files = {"file": ("snap.png", _MIN_PNG_BYTES, "image/png")}
    data = {
        "locker_id": locker_id,
        "kind": "photo",
        "mime_type": "image/png",
        "captured_at": datetime.now(UTC).isoformat(),
        "description": "from device",
        "metadata": "{}",
    }
    resp = _post_multipart_signed("/internal/api/v1/media", files=files, data=data)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "id" in body
    media_id = body["id"]

    detail_resp = api_client.get(f"/api/v1/media/{media_id}")
    assert detail_resp.status_code == 200
    assert detail_resp.headers["Content-Type"] == "image/png"


def test_internal_media_rejects_unsupported_mime():
    files = {"file": ("doc.txt", b"plain text", "text/plain")}
    data = {
        "locker_id": generate_unique_locker_id(),
        "kind": "photo",
        "mime_type": "text/plain",
        "captured_at": datetime.now(UTC).isoformat(),
        "metadata": "{}",
    }
    resp = _post_multipart_signed("/internal/api/v1/media", files=files, data=data)
    assert resp.status_code == 415


def test_internal_media_requires_file():
    resp = _post_multipart_signed(
        "/internal/api/v1/media",
        files=None,
        data={"locker_id": generate_unique_locker_id()},
    )
    assert resp.status_code == 400


# =============================================================================
# SEC-05: HMAC-подпись на internal write-эндпойнтах
# =============================================================================


def _telemetry_payload() -> dict:
    return {
        "readings": [{
            "locker_id": generate_unique_locker_id(),
            "sensor_id": generate_unique_sensor_id(),
            "sensor_type_code": "temperature",
            "value": 1.0,
        }],
    }


def _security_payload() -> dict:
    return {
        "locker_id": generate_unique_locker_id(),
        "event_type": "intrusion",
        "severity": "high",
    }


def test_internal_telemetry_unsigned_rejected_when_key_configured():
    """Без подписи (ключ задан) → 401. Если ключ не сконфигурирован — skip."""
    if _HMAC_KEY is None:
        return
    body = _json.dumps(_telemetry_payload()).encode("utf-8")
    resp = requests.post(
        f"{BASE_URL}/internal/api/v1/telemetry",
        data=body,
        headers={"Content-Type": "application/json"},  # без X-Internal-*
    )
    assert resp.status_code == 401, resp.text


def test_internal_security_unsigned_rejected_when_key_configured():
    if _HMAC_KEY is None:
        return
    body = _json.dumps(_security_payload()).encode("utf-8")
    resp = requests.post(
        f"{BASE_URL}/internal/api/v1/events/security",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 401, resp.text


def test_internal_telemetry_tampered_signature_rejected():
    if _HMAC_KEY is None:
        return
    body = _json.dumps(_telemetry_payload()).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        **_sign_headers("POST", "/internal/api/v1/telemetry", body),
    }
    # Подменяем body после подписи — подпись больше не совпадает.
    tampered = _json.dumps(_telemetry_payload()).encode("utf-8")
    resp = requests.post(
        f"{BASE_URL}/internal/api/v1/telemetry", data=tampered, headers=headers,
    )
    assert resp.status_code == 401, resp.text


def test_internal_signed_telemetry_accepted():
    resp = _post_no_auth("/internal/api/v1/telemetry", _telemetry_payload())
    assert resp.status_code == 201, resp.text


# =============================================================================
# ARC-02: идемпотентность по event_uid (повторная доставка = no-op)
# =============================================================================


def test_security_event_duplicate_event_uid_deduped(api_client):
    """Два POST с одинаковым event_uid → ровно одна строка в БД."""
    locker_id = generate_unique_locker_id()
    event_uid = generate_unique_command_id()  # любой uuid4
    payload = {
        "locker_id": locker_id,
        "event_type": "intrusion",
        "severity": "high",
        "event_uid": event_uid,
    }

    r1 = _post_no_auth("/internal/api/v1/events/security", payload)
    assert r1.status_code == 201, r1.text

    r2 = _post_no_auth("/internal/api/v1/events/security", payload)
    # Дубликат → идемпотентный 200 (а не новая 201-строка).
    assert r2.status_code == 200, r2.text
    assert r2.json().get("status") == "duplicate"

    # В БД ровно одна строка с этим locker_id.
    list_resp = api_client.get(
        f"/api/v1/events/security?locker_id={locker_id}"
    )
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1, items


def test_security_event_dedup_by_event_uid_for_late_execution(api_client):
    """command_late_execution: дубли одного позднего rep (один event_uid) → одна строка."""
    locker_id = generate_unique_locker_id()
    payload = {
        "locker_id": locker_id,
        "event_type": "command_late_execution",
        "severity": "high",
        "violated_thresholds": [{"description": "Команда «открыть» выполнена с опозданием"}],
        "metadata": {"late_kind": "after_failure", "delay_s": 734},
        "event_uid": generate_unique_command_id(),
    }

    assert _post_no_auth("/internal/api/v1/events/security", payload).status_code == 201
    r2 = _post_no_auth("/internal/api/v1/events/security", payload)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("status") == "duplicate"

    list_resp = api_client.get(
        f"/api/v1/events/security?locker_id={locker_id}&event_type=command_late_execution"
    )
    assert list_resp.status_code == 200, list_resp.text
    items = list_resp.json()["items"]
    assert len(items) == 1, items
    assert items[0]["event_type"] == "command_late_execution"
    assert items[0]["metadata"]["late_kind"] == "after_failure"


def test_offline_event_duplicate_event_uid_deduped(api_client):
    """ARC-02: повторная доставка offline-события (тот же event_uid) → одна строка.

    Offline replay (переподключение реле / повторная outbox-доставка) — главный
    кейс дублей. Тело — ровно то, что шлёт OfflineEventProducer.publish_offline.
    """
    locker_id = generate_unique_locker_id()
    event_uid = generate_unique_command_id()
    payload = {
        "locker_id": locker_id,
        "event_type": "offline",
        "severity": "medium",
        "metadata": {"reason": "heartbeat_timeout"},
        "event_uid": event_uid,
    }

    r1 = _post_no_auth("/internal/api/v1/events/security", payload)
    assert r1.status_code == 201, r1.text

    r2 = _post_no_auth("/internal/api/v1/events/security", payload)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("status") == "duplicate"

    list_resp = api_client.get(f"/api/v1/events/security?locker_id={locker_id}")
    assert list_resp.status_code == 200
    items = list_resp.json()["items"]
    assert len(items) == 1, items


def test_telemetry_inserted_count_excludes_deduped(api_client):
    """ARC-02: `inserted` отражает РЕАЛЬНО вставленные строки.

    Батч из двух readings, один из которых дублирует уже записанный event_uid:
    вставляется только новая строка → inserted == 1 (а не 2).
    """
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()
    recorded_at = datetime.now(UTC).isoformat()
    dup_uid = generate_unique_command_id()

    first = _post_no_auth("/internal/api/v1/telemetry", {"readings": [{
        "locker_id": locker_id,
        "sensor_id": sensor_id,
        "sensor_type_code": "temperature",
        "value": 1.0,
        "recorded_at": recorded_at,
        "event_uid": dup_uid,
    }]})
    assert first.status_code == 201, first.text
    assert first.json()["inserted"] == 1

    # Второй батч: один дубль (dup_uid) + один новый reading.
    batch = _post_no_auth("/internal/api/v1/telemetry", {"readings": [
        {
            "locker_id": locker_id,
            "sensor_id": sensor_id,
            "sensor_type_code": "temperature",
            "value": 1.0,
            "recorded_at": recorded_at,
            "event_uid": dup_uid,  # дубль → не вставится
        },
        {
            "locker_id": locker_id,
            "sensor_id": sensor_id,
            "sensor_type_code": "temperature",
            "value": 2.0,
            "recorded_at": recorded_at,
            "event_uid": generate_unique_command_id(),  # новый → вставится
        },
    ]})
    assert batch.status_code == 201, batch.text
    assert batch.json()["inserted"] == 1, batch.text

    list_resp = api_client.get(f"/api/v1/telemetry/list?locker_id={locker_id}")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 2


def test_telemetry_duplicate_event_uid_deduped(api_client):
    """Повторная доставка батча с теми же event_uid → без дублей строк."""
    locker_id = generate_unique_locker_id()
    sensor_id = generate_unique_sensor_id()
    recorded_at = datetime.now(UTC).isoformat()
    reading = {
        "locker_id": locker_id,
        "sensor_id": sensor_id,
        "sensor_type_code": "temperature",
        "value": 42.0,
        "recorded_at": recorded_at,
        "event_uid": generate_unique_command_id(),
    }
    payload = {"readings": [reading]}

    r1 = _post_no_auth("/internal/api/v1/telemetry", payload)
    assert r1.status_code == 201, r1.text
    r2 = _post_no_auth("/internal/api/v1/telemetry", payload)
    assert r2.status_code == 201, r2.text  # ON CONFLICT DO NOTHING — тоже 201

    list_resp = api_client.get(f"/api/v1/telemetry/list?locker_id={locker_id}")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1


def test_media_duplicate_event_uid_deduped(api_client):
    """Повторная доставка медиа с тем же event_uid → 200 duplicate, без дублей."""
    locker_id = generate_unique_locker_id()
    event_uid = generate_unique_command_id()
    files = {"file": ("snap.png", _MIN_PNG_BYTES, "image/png")}
    data = {
        "locker_id": locker_id,
        "kind": "photo",
        "mime_type": "image/png",
        "captured_at": datetime.now(UTC).isoformat(),
        "metadata": "{}",
        "event_uid": event_uid,
    }
    r1 = _post_multipart_signed("/internal/api/v1/media", files=files, data=data)
    assert r1.status_code == 201, r1.text

    files2 = {"file": ("snap.png", _MIN_PNG_BYTES, "image/png")}
    r2 = _post_multipart_signed("/internal/api/v1/media", files=files2, data=data)
    assert r2.status_code == 200, r2.text
    assert r2.json().get("status") == "duplicate"

    list_resp = api_client.get(f"/api/v1/media?locker_id={locker_id}")
    assert list_resp.status_code == 200
    assert list_resp.json()["total"] == 1


# =============================================================================
# Public command_history idempotency
# =============================================================================


def test_public_command_history_post_is_idempotent(api_client):
    """Публичный POST /api/v1/commands/history идемпотентен (нет 409)."""
    locker_id = generate_unique_locker_id()
    command_id = generate_unique_command_id()

    base = {
        "command_id": command_id,
        "locker_id": locker_id,
        "command_type": "open",
    }

    r1 = api_client.post("/api/v1/commands/history", json={**base, "status": "pending"})
    assert r1.status_code == 200
    record_id = r1.json()["id"]

    r2 = api_client.post("/api/v1/commands/history", json={**base, "status": "executed"})
    assert r2.status_code == 200
    assert r2.json()["id"] == record_id
    assert r2.json()["status"] == "executed"
