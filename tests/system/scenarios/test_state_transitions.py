"""Сценарий: переходы состояний шкафа и подавление телеметрии/alarm.

Проверяет:
1. Подключение/отключение релейного двойника не сбрасывает
   operational_status и door.
2. Реконнект синхронизирует door из retained stat нового двойника, но не
   затирает соседние колонки шкафа (operational_status, last_opened_at).
3. Телеметрия battery подавляется в maintenance; alarm по нарушенному
   порогу — тоже.
4. Открытая дверь не подавляет телеметрию остальных датчиков.

Зависимости:
- locker_api: connectivity/door/operational_status, StateMonitor, mqtt-ingest
  (RELAY_OP_STATUS_TTL_S — TTL кэша operational_status на hot path).
- RelayRunner: релейный двойник, публикующий stat и исполняющий com-команды.
"""

from __future__ import annotations

import logging
import time

from utils.api_client import GatewayClient
from utils.mqtt_locker import _create_mqtt_locker, _random_relay_id
from utils.relay_runner import RelayRunner
from utils.wait import wait_for

logger = logging.getLogger(__name__)

_ONLINE_TIMEOUT_S = 45.0
# Запас под тик command-queue процессора (.env.fast: 2с) + доставку rep.
_COMMAND_TIMEOUT_S = 60.0
# fast-mode: MQTT_OFFLINE_TIMEOUT_S=40 / MQTT_OFFLINE_CHECK_INTERVAL_S=3
# (backend/.env.fast) — офлайн-монитор укладывается в этот бюджет.
_OFFLINE_TIMEOUT_S = 70.0
# stat обрабатывается инлайн в ingest (не через тик очереди), поэтому короткого
# окна достаточно, чтобы отличить подавленную телеметрию от ещё не доехавшей.
_NO_TELEMETRY_WINDOW_S = 5.0
# fast-mode: RELAY_OP_STATUS_TTL_S=1 (backend/.env.fast) — запас на истечение
# TTL кэша operational_status в ingest.py перед публикацией stat, который
# должен быть подавлен: иначе кэш ещё может отдавать значение normal,
# закешированное до перехода в maintenance.
_OP_STATUS_CACHE_EXPIRY_S = 2.0


def _sensor_by_code(gateway: GatewayClient, locker_id: str, code: str) -> dict | None:
    """Найти датчик по sensor_types.code среди датчиков шкафа."""
    return next(
        (s for s in gateway.list_sensors(locker_id) if s.get("code") == code),
        None,
    )


def _wait_for_battery(gateway: GatewayClient, locker_id: str) -> dict:
    """Дождаться, пока датчик battery появится лениво и получит значение из stat."""
    wait_for(
        lambda: (
            (sensor := _sensor_by_code(gateway, locker_id, "battery")) is not None
            and sensor.get("current_value") is not None
        ),
        timeout=_ONLINE_TIMEOUT_S,
        interval=1,
        message=f"battery sensor for locker {locker_id} was not lazily created from stat",
    )
    sensor = _sensor_by_code(gateway, locker_id, "battery")
    assert sensor is not None
    return sensor


def _value_never_changes(getter, expected: object, duration: float, interval: float = 1.0) -> bool:
    """Опрашивать getter() до истечения duration; False при первом отклонении от expected."""
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if getter() != expected:
            return False
        time.sleep(interval)
    return True


# ─── Переходы состояний при подключении/отключении ────────────────────────────


def test_connect_preserves_maintenance(gateway: GatewayClient):
    """Подключение двойника (первый retained stat) не сбрасывает
    operational_status=maintenance."""
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]

    try:
        gateway.set_operational_status(locker_id, "maintenance")
        assert gateway.get_operational_status(locker_id) == "maintenance"

        with RelayRunner(relay_id):
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
            assert gateway.get_operational_status(locker_id) == "maintenance"
    finally:
        gateway.delete_locker(locker_id)


def test_disconnect_preserves_maintenance(gateway: GatewayClient):
    """Отключение двойника (молчание stat → offline-монитор) не сбрасывает
    operational_status=maintenance."""
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]

    try:
        gateway.set_operational_status(locker_id, "maintenance")

        with RelayRunner(relay_id):
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
        # RelayRunner stopped here (context exited): device disconnected.

        wait_for(
            lambda: gateway.get_connectivity(locker_id) == "offline",
            timeout=_OFFLINE_TIMEOUT_S,
            interval=2,
            message=f"locker {locker_id} did not flip offline after disconnect",
        )
        assert gateway.get_operational_status(locker_id) == "maintenance"
    finally:
        gateway.delete_locker(locker_id)


def test_disconnect_preserves_door_open(gateway: GatewayClient):
    """Отключение двойника не сбрасывает door=open, зафиксированный
    исполненной командой open."""
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]

    try:
        with RelayRunner(relay_id):
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )

            gateway.send_command(locker_id, "open")
            wait_for(
                lambda: gateway.get_door(locker_id) == "open",
                timeout=_COMMAND_TIMEOUT_S,
                interval=2,
                message="door did not open",
            )

        wait_for(
            lambda: gateway.get_connectivity(locker_id) == "offline",
            timeout=_OFFLINE_TIMEOUT_S,
            interval=2,
            message=f"locker {locker_id} did not flip offline after disconnect",
        )
        assert gateway.get_door(locker_id) == "open"
    finally:
        gateway.delete_locker(locker_id)


def test_reconnect_preserves_door(gateway: GatewayClient):
    """Реконнект не затирает СОСЕДНИЕ колонки шкафа (operational_status,
    last_opened_at) — трогает только door.

    На релейном пути door приходит в каждом retained stat авторитетно
    (``sync_door_state`` в ingest.py): новый процесс двойника для того же
    relay_id стартует с door=CLOSED. Сразу после подписки брокер доставляет
    ему retained LOCK_OPEN из фазы 1 (com/{id} публикуется с retain), и
    двойник переисполнил бы его через ~40 мс — door снова стал бы open, а
    результат теста зависел бы от того, успел ли опрос попасть в это окно.
    Поэтому у переподключённого двойника дверь заблокирована: replay получает
    REP_FAILURE, и door меняет только stat на подключении. Реальный инвариант —
    что sync_door_state трогает ТОЛЬКО столбец door и не задевает
    operational_status/last_opened_at, установленные до реконнекта.
    """
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]

    try:
        with RelayRunner(relay_id):
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online (1st)",
            )

            gateway.send_command(locker_id, "open")
            wait_for(
                lambda: gateway.get_door(locker_id) == "open",
                timeout=_COMMAND_TIMEOUT_S,
                interval=2,
                message="door did not open before reconnect",
            )
            last_opened_at = gateway.get_locker(locker_id)["last_opened_at"]
            assert last_opened_at is not None

        wait_for(
            lambda: gateway.get_connectivity(locker_id) == "offline",
            timeout=_OFFLINE_TIMEOUT_S,
            interval=2,
            message=f"locker {locker_id} did not flip offline before reconnect",
        )

        gateway.set_operational_status(locker_id, "maintenance")

        reconnected = RelayRunner(relay_id)
        reconnected.device.door_locked = True
        with reconnected:
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come back online on reconnect",
            )
            wait_for(
                lambda: gateway.get_door(locker_id) == "closed",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message="door was not synced from the reconnected device's stat",
            )
            assert _value_never_changes(
                lambda: gateway.get_door(locker_id),
                "closed",
                _NO_TELEMETRY_WINDOW_S,
            ), "door left 'closed' after reconnect (retained command replay)"

            state = gateway.get_locker(locker_id)
            assert state["operational_status"] == "maintenance", (
                f"reconnect clobbered operational_status: {state}"
            )
            assert state["last_opened_at"] == last_opened_at, (
                f"reconnect clobbered last_opened_at: {state}"
            )
    finally:
        gateway.delete_locker(locker_id)


# ─── Подавление телеметрии/alarm ───────────────────────────────────────────────


def test_telemetry_suppressed_in_maintenance(gateway: GatewayClient):
    """В режиме обслуживания stat не обновляет current_value датчика battery."""
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]

    try:
        with RelayRunner(relay_id, battery=22) as runner:
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
            sensor = _wait_for_battery(gateway, locker_id)
            sensor_id = sensor["id"]
            assert sensor["current_value"] == 22

            gateway.set_operational_status(locker_id, "maintenance")
            time.sleep(_OP_STATUS_CACHE_EXPIRY_S)

            runner.device.battery = 99
            runner.publish_stat()

            assert _value_never_changes(
                lambda: gateway.get_sensor(locker_id, sensor_id)["current_value"],
                22,
                _NO_TELEMETRY_WINDOW_S,
            ), "telemetry was written during maintenance"
    finally:
        gateway.delete_locker(locker_id)


def test_non_door_telemetry_flows_when_door_open(gateway: GatewayClient):
    """Открытая дверь не подавляет телеметрию остальных датчиков: stat с новым
    battery продолжает обновлять current_value, пока door=open (подавление
    в ingest.py завязано только на maintenance, не на door)."""
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]

    try:
        with RelayRunner(relay_id, battery=22) as runner:
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
            sensor = _wait_for_battery(gateway, locker_id)
            sensor_id = sensor["id"]
            assert sensor["current_value"] == 22

            gateway.send_command(locker_id, "open")
            wait_for(
                lambda: gateway.get_door(locker_id) == "open",
                timeout=_COMMAND_TIMEOUT_S,
                interval=2,
                message="door did not open",
            )

            runner.device.battery = 99
            runner.publish_stat()

            wait_for(
                lambda: gateway.get_sensor(locker_id, sensor_id)["current_value"] == 99,
                timeout=15,
                interval=1,
                message="battery telemetry was suppressed while door was open",
            )
    finally:
        gateway.delete_locker(locker_id)


def test_maintenance_to_normal_transition(gateway: GatewayClient):
    """Возврат operational_status maintenance → normal возобновляет запись
    телеметрии battery.

    Регрессия на TTL кэша ``_op_status`` в ingest.py (RELAY_OP_STATUS_TTL_S):
    без короткого TTL кэш может ещё отдавать maintenance после перехода в
    normal, и следующий stat молча подавляется.
    """
    relay_id = _random_relay_id()
    locker = _create_mqtt_locker(gateway, relay_id)
    locker_id = locker["id"]

    try:
        with RelayRunner(relay_id, battery=22) as runner:
            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "online",
                timeout=_ONLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not come online",
            )
            sensor = _wait_for_battery(gateway, locker_id)
            sensor_id = sensor["id"]
            assert sensor["current_value"] == 22

            gateway.set_operational_status(locker_id, "maintenance")
            time.sleep(_OP_STATUS_CACHE_EXPIRY_S)
            runner.device.battery = 50
            runner.publish_stat()

            assert _value_never_changes(
                lambda: gateway.get_sensor(locker_id, sensor_id)["current_value"],
                22,
                _NO_TELEMETRY_WINDOW_S,
            ), "telemetry was written during maintenance"

            gateway.set_operational_status(locker_id, "normal")
            time.sleep(_OP_STATUS_CACHE_EXPIRY_S)
            runner.device.battery = 77
            runner.publish_stat()

            wait_for(
                lambda: gateway.get_sensor(locker_id, sensor_id)["current_value"] == 77,
                timeout=15,
                interval=1,
                message="telemetry not resumed after exiting maintenance",
            )
    finally:
        gateway.delete_locker(locker_id)
