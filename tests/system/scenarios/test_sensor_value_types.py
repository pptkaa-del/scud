"""Сценарий: типобезопасность значений датчиков релейного двойника.

``sensors.current_value`` хранится в JSONB-обёртке ``{"v": <typed_value>}``;
API обязан отдавать и принимать значение каждого типа без потери типа (bool
остаётся ``bool``, а не строкой ``"true"``/числом ``1.0``; numeric остаётся
``int``/``float``, а не ``bool`` — он подкласс ``int`` в Python).

Единственный источник телеметрии релейного транспорта — кадр ``stat``
(``RelayDevice.build_stat``). Один такой кадр лениво создаёт сразу несколько
датчиков: ``battery``/``signal``/``power``/``net_type`` (numeric) и
``door``/``error`` (boolean) — этого достаточно, чтобы проверить типобезопасность
хранения и выдачи через API.

Покрывает:
1. Несколько ``value_type`` на одном кадре ``stat``, без cross-type contamination.
2. Числовое нарушение порога, доезжающее до ``history_api.security_events``.
3. Boolean round-trip через HTTP и нормализация ``fixed_value``
   (``ThresholdRepository._normalize_for_compare``).
"""

from __future__ import annotations

import logging
import time

from utils.api_client import GatewayClient
from utils.mqtt_locker import _create_mqtt_locker, _random_relay_id
from utils.relay_runner import RelayRunner
from utils.timing import state_monitor_cycles
from utils.wait import wait_for

logger = logging.getLogger(__name__)

_ONLINE_TIMEOUT_S = 45.0
_ALARM_TIMEOUT_S = state_monitor_cycles(2, buffer_s=20)
_EVENT_TIMEOUT_S = 30.0
_NO_ALARM_WAIT_S = state_monitor_cycles(1, buffer_s=2)


def _sensor_by_code(gateway: GatewayClient, locker_id: str, code: str) -> dict | None:
    """Найти датчик по ``sensor_types.code`` среди датчиков шкафа."""
    return next(
        (s for s in gateway.list_sensors(locker_id) if s.get("code") == code),
        None,
    )


def _wait_for_sensor(gateway: GatewayClient, locker_id: str, code: str) -> dict:
    """Дождаться, пока датчик ``code`` появится лениво и получит значение из stat."""
    wait_for(
        lambda: (
            (sensor := _sensor_by_code(gateway, locker_id, code)) is not None
            and sensor.get("current_value") is not None
        ),
        timeout=_ONLINE_TIMEOUT_S,
        interval=1,
        message=f"sensor {code!r} for locker {locker_id} was not lazily created from stat",
    )
    sensor = _sensor_by_code(gateway, locker_id, code)
    assert sensor is not None
    return sensor


# ── Хранение типов значений ──────────────────────────────────────────────────


class TestSensorValueStorage:
    """Хранение и возврат значений разных типов через locker_api."""

    def test_stat_frame_lazily_creates_typed_sensors(self, gateway: GatewayClient):
        """Один v2-кадр stat создаёт battery/signal/power (numeric) и door
        (boolean) — значения хранятся и отдаются API независимо друг от друга,
        без cross-type contamination."""
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info("Created mqtt locker id=%s relay_id=%s", locker_id, relay_id)

        try:
            with RelayRunner(relay_id, battery=77, signal=42) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )

                battery = _wait_for_sensor(gateway, locker_id, "battery")
                signal = _wait_for_sensor(gateway, locker_id, "signal")
                power = _wait_for_sensor(gateway, locker_id, "power")
                door = _wait_for_sensor(gateway, locker_id, "door")

                for sensor in (battery, signal, power):
                    value = sensor["current_value"]
                    assert isinstance(value, (int, float)) and not isinstance(value, bool), (
                        f"{sensor['code']}: expected numeric, got "
                        f"{type(value).__name__}: {value!r}"
                    )
                assert battery["current_value"] == 77
                assert signal["current_value"] == 42
                # PowerSource.MAINS=1 (relay_protocol.py) — RelayDevice.power default.
                assert power["current_value"] == 1

                door_value = door["current_value"]
                assert isinstance(door_value, bool), (
                    f"door: expected bool, got {type(door_value).__name__}: {door_value!r}"
                )
                assert door_value is False  # RelayDevice default door=Door.CLOSED

                # current_value остаётся сырым значением протокола, подпись — в display.
                assert power["display"] == {"label": "От сети", "severity": "normal"}
                assert power["unit"] is None
                assert door["display"] == {"label": "Закрыта", "severity": "normal"}
                assert battery["display"] is None
                assert signal["display"] is None

                net_type = _wait_for_sensor(gateway, locker_id, "net_type")
                assert net_type["current_value"] == 1  # RelayDevice default net_type
                assert net_type["display"] == {"label": "Ethernet", "severity": "normal"}

                error = _wait_for_sensor(gateway, locker_id, "error")
                assert error["current_value"] is False
                assert error["display"] == {"label": "Норма", "severity": "normal"}

                logger.info(
                    "relay-%s stat-frame sensors: battery=%r signal=%r power=%r door=%r",
                    relay_id, battery["current_value"], signal["current_value"],
                    power["current_value"], door_value,
                )
        finally:
            gateway.delete_locker(locker_id)


# ── History (security_events) ────────────────────────────────────────────────


class TestSecurityEventHistory:
    """Запись события безопасности в history_api при численном нарушении порога."""

    def test_numeric_violation_creates_security_event(self, gateway: GatewayClient):
        """signal выше max_value → alarm + threshold_violation в history с
        корректными locker_id/sensor_id/severity/violated_thresholds — числовое
        значение доезжает до history без потери типа."""
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info("Created mqtt locker id=%s relay_id=%s", locker_id, relay_id)

        try:
            with RelayRunner(relay_id, signal=10) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )

                sensor = _wait_for_sensor(gateway, locker_id, "signal")
                sensor_id = sensor["id"]
                assert sensor["current_value"] == 10

                gateway.create_threshold(
                    locker_id=locker_id, sensor_id=sensor_id, max_val=50.0,
                )

                runner.device.signal = 90
                runner.publish_stat()
                logger.info(
                    "relay-%s published signal=90 (above max_value=50)", relay_id
                )

                wait_for(
                    lambda: gateway.get_operational_status(locker_id) == "alarm",
                    timeout=_ALARM_TIMEOUT_S,
                    interval=1,
                    message=(
                        f"locker {locker_id} did not enter alarm after signal "
                        f"threshold violation"
                    ),
                )

                wait_for(
                    lambda: any(
                        e.get("event_type") == "threshold_violation"
                        and e.get("locker_id") == locker_id
                        for e in gateway.get_security_events(
                            locker_id=locker_id, event_type="threshold_violation",
                        )
                    ),
                    timeout=_EVENT_TIMEOUT_S,
                    interval=2,
                    message=f"threshold_violation event for {locker_id} did not reach history_api",
                )
                events = [
                    e for e in gateway.get_security_events(
                        locker_id=locker_id, event_type="threshold_violation",
                    )
                    if e.get("event_type") == "threshold_violation"
                ]
                assert events, "threshold_violation event not found after wait"
                event = events[-1]
                logger.info("Security event: %s", event)

                assert event["locker_id"] == locker_id
                assert event["event_type"] == "threshold_violation"
                assert event["severity"] == "high"
                assert event.get("sensor_id") == sensor_id
                violations = event.get("violated_thresholds")
                assert isinstance(violations, list) and len(violations) > 0, (
                    f"violated_thresholds empty/missing: {event}"
                )
        finally:
            gateway.delete_locker(locker_id)


# ── Threshold-проверки для boolean ───────────────────────────────────────────


class TestBooleanThreshold:
    """Boolean round-trip через HTTP и normalize_for_compare (fixed_value)."""

    def test_boolean_round_trip_and_threshold_normalization(self, gateway: GatewayClient):
        """door=false (дверь закрыта) круглым рейсом через HTTP: тип bool не
        теряется, а normalize_for_compare матчит его с текстовым fixed_value.

        Дверь держим ЗАКРЫТОЙ весь тест: StateMonitor намеренно исключает
        door-датчик из порогового обхода, пока locker.door='open' (см.
        state_monitor.py: `AND NOT (stype.code='door' AND l.door='open')`) —
        при открытой двери пороговая проверка на этом датчике не выполняется
        вовсе, и тест не смог бы отличить корректную нормализацию от
        отсутствующей проверки.
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info("Created mqtt locker id=%s relay_id=%s", locker_id, relay_id)

        try:
            with RelayRunner(relay_id) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )

                sensor = _wait_for_sensor(gateway, locker_id, "door")
                sensor_id = sensor["id"]
                value = sensor["current_value"]
                assert isinstance(value, bool), (
                    f"door: expected bool via HTTP, got {type(value).__name__}: {value!r}"
                )
                assert value is False  # RelayDevice default door=Door.CLOSED

                # Регрессия на normalize_for_compare: bool False должен совпасть
                # с текстовым fixed_value "false", а не str(False)="False".
                gateway.create_threshold(
                    locker_id=locker_id, sensor_id=sensor_id, fixed_val="false",
                )
                time.sleep(_NO_ALARM_WAIT_S)
                assert gateway.get_operational_status(locker_id) != "alarm", (
                    "ALARM сработал на совпадающем boolean=false / fixed_value='false'"
                )

                # Второй, несовпадающий порог на том же датчике обязан
                # сработать — иначе предыдущая проверка ничего не доказывает
                # (StateMonitor мог бы просто никогда не алармить boolean).
                gateway.create_threshold(
                    locker_id=locker_id, sensor_id=sensor_id, fixed_val="true",
                )
                wait_for(
                    lambda: gateway.get_operational_status(locker_id) == "alarm",
                    timeout=_ALARM_TIMEOUT_S,
                    interval=1,
                    message=(
                        f"locker {locker_id} did not enter alarm after mismatched "
                        f"boolean threshold"
                    ),
                )
        finally:
            gateway.delete_locker(locker_id)


# ── Подписи состояний из sensor_types.states ─────────────────────────────────


class TestSensorStateLabels:
    """Подпись, заданная администратором в states типа, видна в display датчика."""

    def test_admin_edited_label_shows_in_display(self, gateway: GatewayClient):
        power_type = next(t for t in gateway.get_sensor_types() if t["code"] == "power")
        original_states = power_type["states"]
        assert original_states, "power type has no states to restore"

        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            gateway.set_sensor_type_states(power_type["id"], [
                {"value": 0, "label": "Батарея", "severity": "alert"},
                {"value": 1, "label": "Сеть", "severity": "normal"},
            ])
            with RelayRunner(relay_id):
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )
                _wait_for_sensor(gateway, locker_id, "power")
                wait_for(
                    lambda: (_sensor_by_code(gateway, locker_id, "power") or {}).get("display")
                    == {"label": "Сеть", "severity": "normal"},
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=1,
                    message="power display did not pick up the edited label",
                )
        finally:
            gateway.set_sensor_type_states(power_type["id"], original_states)
            gateway.delete_locker(locker_id)
