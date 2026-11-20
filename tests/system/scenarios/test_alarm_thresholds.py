"""Сценарий: пороговые тревоги шкафа на релейном двойнике.

Гоняет боевой sweep ``StateMonitor.start_monitoring`` (JOIN sensors x
sensor_thresholds x sensor_types x lockers) против настоящей схемы БД, а не
мока — юнит-тесты locker_api сравнивают лишь текст SQL-запроса и регрессию
предиката не ловят.

Проверяет:
1. Нарушение порога battery переводит шкаф в alarm и создаёт persistent
   уведомление threshold_violation с корректными locker_id/sensor_id.
2. Восстановление значения датчика само по себе alarm не снимает — снять его
   может только оператор явным сбросом operational_status в normal (см.
   state_monitor.py: шкаф в alarm исключён из sweep). После сброса, пока
   значение уже в норме, следующий цикл sweep не поднимает alarm заново.
3. В maintenance нарушение порога вовсе не переводит шкаф в alarm.

Зависимости:
- locker_api: sensor_thresholds, StateMonitor sweep (интервал —
  STATE_MONITOR_CHECK_INTERVAL_S).
- notification_api: persistent-уведомления threshold_violation.
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
_NO_ALARM_WAIT_S = state_monitor_cycles(1, buffer_s=2)


def _battery_sensor(gateway: GatewayClient, locker_id: str) -> dict | None:
    """Найти датчик battery среди датчиков шкафа."""
    return next(
        (s for s in gateway.list_sensors(locker_id) if s.get("code") == "battery"),
        None,
    )


def _wait_for_battery(gateway: GatewayClient, locker_id: str) -> dict:
    """Дождаться, пока датчик battery появится лениво и получит значение из stat."""
    wait_for(
        lambda: (
            (sensor := _battery_sensor(gateway, locker_id)) is not None
            and sensor.get("current_value") is not None
        ),
        timeout=_ONLINE_TIMEOUT_S,
        interval=1,
        message=f"battery sensor for locker {locker_id} was not lazily created from stat",
    )
    sensor = _battery_sensor(gateway, locker_id)
    assert sensor is not None
    return sensor


def test_battery_threshold_violation_triggers_alarm(gateway: GatewayClient):
    """Battery ниже порога → шкаф в alarm + persistent-уведомление threshold_violation."""
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]
    logger.info("Created mqtt locker id=%s relay_id=%s", locker_id, relay_id)

    try:
        with RelayRunner(relay_id, battery=100) as runner:
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
            sensor = _wait_for_battery(gateway, locker_id)
            sensor_id = sensor["id"]
            assert sensor["current_value"] == 100

            gateway.create_threshold(locker_id=locker_id, sensor_id=sensor_id, min_val=20.0)

            runner.device.battery = 5
            runner.publish_stat()
            logger.info("relay-%s published battery=5 (below min_value=20)", relay_id)

            wait_for(
                lambda: gateway.get_operational_status(locker_id) == "alarm",
                timeout=_ALARM_TIMEOUT_S,
                interval=1,
                message=(
                    f"locker {locker_id} did not enter alarm after battery "
                    f"threshold violation"
                ),
            )

            wait_for(
                lambda: len(gateway.get_all_notifications_by_locker(
                    locker_id, event="threshold_violation"
                )) > 0,
                timeout=30,
                interval=2,
                message="threshold_violation notification not created",
            )
            notif = gateway.get_all_notifications_by_locker(
                locker_id, event="threshold_violation"
            )[0]
            tags = notif["tags"]
            assert tags["locker_id"] == locker_id, f"wrong locker_id in tags: {tags}"
            assert tags["sensor_id"] == sensor_id, f"wrong sensor_id in tags: {tags}"
    finally:
        gateway.delete_locker(locker_id)


def test_alarm_cleared_when_value_returns_to_normal(gateway: GatewayClient):
    """Возврат значения в норму + сброс оператором → шкаф не алярмит заново.

    Пока шкаф в alarm, sweep исключает его строки предикатом
    ``operational_status NOT IN (maintenance, alarm)`` (state_monitor.py) —
    восстановление датчика само по себе статус не меняет, снять alarm может
    только оператор явным сбросом в normal. Проверяем сам предикат: после
    сброса, при уже нормальном значении, следующий цикл sweep не поднимает
    alarm заново.
    """
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]
    logger.info("Created mqtt locker id=%s relay_id=%s", locker_id, relay_id)

    try:
        with RelayRunner(relay_id, battery=100) as runner:
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
            sensor = _wait_for_battery(gateway, locker_id)
            sensor_id = sensor["id"]

            gateway.create_threshold(locker_id=locker_id, sensor_id=sensor_id, min_val=20.0)

            runner.device.battery = 5
            runner.publish_stat()
            wait_for(
                lambda: gateway.get_operational_status(locker_id) == "alarm",
                timeout=_ALARM_TIMEOUT_S,
                interval=1,
                message=(
                    f"locker {locker_id} did not enter alarm after battery "
                    f"threshold violation"
                ),
            )
            logger.info("relay-%s entered alarm on battery=5", relay_id)

            runner.device.battery = 100
            runner.publish_stat()
            wait_for(
                lambda: gateway.get_sensor(locker_id, sensor_id)["current_value"] == 100,
                timeout=15,
                interval=1,
                message="battery value did not return to normal",
            )

            gateway.set_operational_status(locker_id, "normal")

            time.sleep(_NO_ALARM_WAIT_S)
            assert gateway.get_operational_status(locker_id) == "normal", (
                "locker re-entered alarm after value returned to normal"
            )
    finally:
        gateway.delete_locker(locker_id)


def test_alarm_suppressed_in_maintenance(gateway: GatewayClient):
    """В maintenance нарушение порога battery не переводит шкаф в alarm."""
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]
    logger.info("Created mqtt locker id=%s relay_id=%s", locker_id, relay_id)

    try:
        with RelayRunner(relay_id, battery=100) as runner:
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
            sensor = _wait_for_battery(gateway, locker_id)
            sensor_id = sensor["id"]

            gateway.create_threshold(locker_id=locker_id, sensor_id=sensor_id, min_val=20.0)

            gateway.set_operational_status(locker_id, "maintenance")

            runner.device.battery = 5
            runner.publish_stat()

            time.sleep(_NO_ALARM_WAIT_S)
            assert gateway.get_operational_status(locker_id) == "maintenance", (
                "alarm triggered despite maintenance mode"
            )
    finally:
        gateway.delete_locker(locker_id)
