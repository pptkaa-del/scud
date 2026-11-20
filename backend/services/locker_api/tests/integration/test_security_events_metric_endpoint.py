"""Интеграция: нарушение порога двигает scud_security_events_total на /metrics.

Создаём шкаф + temperature-датчик с current_value=99, ставим max-порог 50.
Устанавливаем connectivity='online' через прямой DB-апдейт (WS-хэндшейк не нужен),
чтобы Guard 3 (offline-suppression) в StateMonitor не блокировал обработку.
Фоновый StateMonitor-sweep ловит нарушение → ThresholdViolationProducer.publish
инкрементит scud_security_events_total{type=threshold_violation, severity=high}.
Поллим /metrics, пока серия не вырастет (в пределах одного sweep-интервала).
"""
from __future__ import annotations

import os
import re
import time
from typing import Any
import uuid

import psycopg2

BASE_URL = os.getenv("TEST_BASE_URL", "http://localhost:8001")
SERVICE = "locker_api"
METRIC = "scud_security_events_total"
EXPECTED_TYPE = "threshold_violation"
EXPECTED_SEVERITY = "high"

# Один sweep = STATE_MONITOR_CHECK_INTERVAL_S (default 60). Ждём с запасом.
_POLL_TIMEOUT_S = int(os.getenv("STATE_MONITOR_CHECK_INTERVAL_S", "60")) * 2 + 10


def _db_conn() -> psycopg2.extensions.connection:
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _set_connectivity_online(locker_id: str) -> None:
    """Установить connectivity='online' через прямой DB-апдейт.

    StateMonitor подавляет нарушения порогов для offline-шкафов (Guard 3).
    Реальные устройства делают это через WS-хэндшейк; в тестах — прямой апдейт.
    """
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE lockers SET connectivity = 'online', last_seen_at = now() WHERE id = %s",
                (locker_id,),
            )
        conn.commit()
    finally:
        conn.close()


def _unique_relay_id() -> int:
    return 500_000_000 + uuid.uuid4().int % 400_000_000


def _hard_delete_locker(locker_id: str) -> None:
    """Hard-delete шкафа для чистоты: каскадно удаляет датчики и пороги."""
    conn = _db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
        conn.commit()
    finally:
        conn.close()


def _series_value(body: str) -> float:
    """Извлечь числовое значение из Prometheus exposition text.

    Label-порядок в тексте не фиксируем — ищем подстроки по каждому label.
    Возвращает 0.0, если серия с нужными label'ами не найдена.
    """
    pattern = re.compile(
        rf"^{re.escape(METRIC)}\{{(?P<labels>[^}}]*)\}}\s+(?P<value>[0-9eE.+-]+)$",
        re.MULTILINE,
    )
    for match in pattern.finditer(body):
        labels = match.group("labels")
        if (
            f'service="{SERVICE}"' in labels
            and f'type="{EXPECTED_TYPE}"' in labels
            and f'severity="{EXPECTED_SEVERITY}"' in labels
        ):
            return float(match.group("value"))
    return 0.0


def test_threshold_violation_moves_security_events_metric(api_client: Any) -> None:
    """Реальный sweep StateMonitor инкрементит scud_security_events_total.

    Сценарий:
      1. Baseline /metrics.
      2. Создать шкаф (connectivity=offline по умолчанию) + temperature-датчик
         с current_value=99 (выше порога).
      3. Задать порог min=-10, max=50 → 99 нарушает.
      4. Установить connectivity='online' напрямую в БД — иначе Guard 3
         в StateMonitor подавит тревогу (шкаф offline).
      5. Поллить /metrics до роста серии (deadline = 2×sweep + 10 с).
      6. Убедиться, что delta >= 1. Cleanup — hard-delete шкафа.
    """
    # 0. Baseline /metrics.
    before = _series_value(api_client.get("/metrics").text)

    inv = f"SEC-{int(time.time() * 1000) % 1_000_000}"
    locker_id: str | None = None

    try:
        # 1. Шкаф + temperature-датчик со значением выше порога.
        locker_resp = api_client.post(
            "/api/v1/lockers",
            json={
                "inventory_number": inv,
                "transport": "mqtt",
                "relay_id": _unique_relay_id(),
            },
        )
        assert locker_resp.status_code == 201, locker_resp.text
        locker_id = locker_resp.json()["id"]

        sensor_resp = api_client.post(
            f"/api/v1/locker/{locker_id}/sensors",
            json={
                "serial_number": f"{inv}-S",
                "sensor_type_id": 1,   # temperature → ThresholdViolationProducer
                "current_value": 99.0,
            },
        )
        assert sensor_resp.status_code == 201, sensor_resp.text
        sensor_id = sensor_resp.json()["id"]

        # 2. Порог max=50 → current_value=99 его нарушает.
        thr_resp = api_client.post(
            f"/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds",
            json={"min_value": -10.0, "max_value": 50.0},
        )
        assert thr_resp.status_code == 201, thr_resp.text

        # 3. Устанавливаем connectivity='online' — Guard 3 StateMonitor подавляет
        #    нарушения порогов для offline-шкафов.
        assert locker_id is not None  # уже проверено выше через assert 201
        _set_connectivity_online(locker_id)

        # 4. Ждём sweep: серия должна вырасти хотя бы на 1.
        deadline = time.time() + _POLL_TIMEOUT_S
        after = before
        while time.time() < deadline:
            after = _series_value(api_client.get("/metrics").text)
            if after - before >= 1.0:
                break
            time.sleep(2)

        assert after - before >= 1.0, (
            f"{METRIC}{{type={EXPECTED_TYPE},severity={EXPECTED_SEVERITY}}} "
            f"не вырос: before={before}, after={after}"
        )

    finally:
        if locker_id is not None:
            _hard_delete_locker(locker_id)
