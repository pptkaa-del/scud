"""Сценарий: end-to-end relay-board (MQTT) поверх живого стека.

Проверяет полный цикл нового MQTT-транспорта с headless-двойником `scud_relay`:

  1. API создаёт + провизионирует mqtt-шкаф
     (POST /api/v1/lockers {transport:"mqtt", relay_id:N}) — backend пишет
     broker-креды relay-{N} в shared config volume, брокер перечитывает по SIGHUP.
  2. Двойник `scud_relay.RelayMqttTransport` подключается к брокеру как relay-{N}
     (username relay-{N}, password sha256("relay-{N}")[:16].hex()), подписывается
     на com/{N} и публикует retained `stat`.
  3. stat ингестится locker_api → connectivity шкафа становится online (poll API).
  4. API отправляет команду open → backend публикует com/{N} →
     устройство применяет LOCK_OPEN → публикует rep → backend помечает команду
     executed (poll command-history) и фиксирует дверь устройства open.

Команды доставляются фоновым command-queue процессором (тик 2s), поэтому путь
команды ждём с запасом (~40s). Шкаф удаляется в finally.

Транспорт и backend-flow уже проверены вручную — этот тест формализует регрессию.
"""

from __future__ import annotations

import logging
import time

from scud_relay.device import Door
from utils.api_client import GatewayClient
from utils.mqtt_locker import _create_mqtt_locker, _random_relay_id
from utils.relay_runner import RelayRunner
from utils.wait import wait_for

logger = logging.getLogger(__name__)

# Большой запас под тик command-queue процессора (2s) + доставку rep.
_COMMAND_TIMEOUT_S = 60.0
_ONLINE_TIMEOUT_S = 45.0
# Offline flip waits on the locker_api offline-monitor sweep. The fast-mode test
# stack runs MQTT_OFFLINE_TIMEOUT_S=40 / CHECK=3 (backend/.env.fast), so a single
# stat keeps a non-heartbeating device online across the (3s) offline-monitor sweep,
# while the offline/reconnect scenarios still flip within this ceiling.
_OFFLINE_TIMEOUT_S = 70.0

# Heartbeat interval for the telemetry scenario, which needs repeated stats to
# exercise the device→ingest→history telemetry path. Command scenarios do NOT
# heartbeat on purpose: each extra stat emits battery/signal telemetry events
# that currently fail at history_api (non-UUID sensor_id backend bug) and clog
# the shared outbound dispatcher, delaying command-history delivery. Keeping
# command scenarios to a single stat keeps that backlog negligible.
_HEARTBEAT_INTERVAL_S = 2.0

# Запас на путь offline-события до history_api после флипа connectivity
# (OfflineEventProducer → outbound dispatcher → history_api).
_EVENT_TIMEOUT_S = 30.0

# stat «Device status» (протокол §5): 0=offline, 1=online, 2=error.
_RELAY_STATUS_OFFLINE = 0

# reason из OfflineEventProducer (backend). Для relay-пути offline всегда
# heartbeat_timeout — и при молчании stat, и при stat=offline
# (см. ingest._emit_offline / offline_monitor._emit_offline).
_OFFLINE_REASON_HEARTBEAT_TIMEOUT = "heartbeat_timeout"

# Описание из ingest._UNAUTHORIZED_OPEN_VIOLATION (протокол платы §7.1).
_UNAUTHORIZED_OPEN_DESCRIPTION = "Дверь открыта без команды «Открыть»"
# Отрицательная проверка: время на путь возможного события до history_api.
_INTRUSION_SETTLE_S = 10.0


class TestRelayMqttE2E:
    """E2E поверх живого стека: API ↔ broker ↔ scud_relay."""

    def test_relay_stat_brings_locker_online(self, gateway: GatewayClient):
        """stat от relay-{id} переводит шкаф в connectivity=online."""
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info(
            "Created mqtt locker id=%s relay_id=%s", locker_id, relay_id
        )

        try:
            with RelayRunner(relay_id) as runner:
                logger.info(
                    "relay-%s authenticated to broker; awaiting online", relay_id
                )
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"locker {locker_id} (relay-{relay_id}) did not come "
                        f"online from stat"
                    ),
                )
                assert gateway.get_connectivity(locker_id) == "online"
                logger.info("locker %s online via relay stat", locker_id)
        finally:
            gateway.delete_locker(locker_id)

    def test_relay_command_executed(self, gateway: GatewayClient):
        """Полный цикл: create → online → open → com → rep → executed.

        Шлёт open через API, ждёт пока команда дойдёт до executed в
        command-history, и проверяет, что дверь двойника открылась (LOCK_OPEN).
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info(
            "Created mqtt locker id=%s relay_id=%s", locker_id, relay_id
        )

        try:
            with RelayRunner(relay_id) as runner:
                # 1. Дождаться online от первичного stat.
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"locker {locker_id} (relay-{relay_id}) did not come "
                        f"online before command"
                    ),
                )
                logger.info("locker %s online; sending open", locker_id)

                # Дверь до команды закрыта (дефолт RelayDevice).
                assert runner.device.door == Door.CLOSED

                # 2. Отправить open через API.
                command = gateway.send_command(locker_id, "open")
                command_id = command["id"]
                logger.info("open command enqueued: id=%s", command_id)

                # 3. Дождаться executed в command-history.
                def open_executed() -> bool:
                    items = gateway.get_command_history(
                        locker_id=locker_id,
                        command_type="open",
                        status="executed",
                        limit=50,
                    )
                    return any(
                        item.get("command_id") == command_id for item in items
                    )

                wait_for(
                    open_executed,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"open command {command_id} did not reach executed "
                        f"for relay-{relay_id}"
                    ),
                )
                logger.info("command %s executed end-to-end", command_id)

                # 4. Дверь двойника открыта (rep сформирован после LOCK_OPEN).
                wait_for(
                    lambda: runner.device.door == Door.OPEN,
                    timeout=10,
                    interval=0.5,
                    message="device door did not transition to OPEN",
                )
                assert runner.device.door == Door.OPEN
                logger.info(
                    "relay-%s door OPEN after executed command", relay_id
                )
        finally:
            gateway.delete_locker(locker_id)

    def test_relay_close_command_executed(self, gateway: GatewayClient):
        """Полный цикл close: create → online → close → com → rep → executed.

        Дверь двойника по дефолту CLOSED — сначала открываем командой open,
        затем шлём close и убеждаемся, что команда дошла до executed и дверь
        двойника снова CLOSED (LOCK_CLOSE).
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info(
            "Created mqtt locker id=%s relay_id=%s", locker_id, relay_id
        )

        try:
            with RelayRunner(relay_id) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"locker {locker_id} (relay-{relay_id}) did not come "
                        f"online before close"
                    ),
                )

                # Открыть, чтобы close был наблюдаемым переходом OPEN→CLOSED.
                runner.device.door = Door.OPEN

                command = gateway.send_command(locker_id, "close")
                command_id = command["id"]
                logger.info("close command enqueued: id=%s", command_id)

                def close_executed() -> bool:
                    items = gateway.get_command_history(
                        locker_id=locker_id,
                        command_type="close",
                        status="executed",
                        limit=50,
                    )
                    return any(
                        item.get("command_id") == command_id for item in items
                    )

                wait_for(
                    close_executed,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"close command {command_id} did not reach executed "
                        f"for relay-{relay_id}"
                    ),
                )
                logger.info("close command %s executed end-to-end", command_id)

                wait_for(
                    lambda: runner.device.door == Door.CLOSED,
                    timeout=10,
                    interval=0.5,
                    message="device door did not transition to CLOSED",
                )
                assert runner.device.door == Door.CLOSED
                logger.info(
                    "relay-%s door CLOSED after executed close", relay_id
                )
        finally:
            gateway.delete_locker(locker_id)

    def test_relay_not_supported_command_fails(self, gateway: GatewayClient):
        """NOT_SUPPORTED rep → команда становится failed (не executed).

        Запускаем v1-устройство: LOCK_OPEN на v1 возвращает NOT_SUPPORTED.
        Шлём open через API → двойник отвечает rep с result=NOT_SUPPORTED →
        backend помечает команду failed (путь RelayCommandResults), дверь не
        меняется (v1 stat не несёт door).
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info(
            "Created mqtt locker id=%s relay_id=%s (v1)", locker_id, relay_id
        )

        try:
            with RelayRunner(relay_id, version=1) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"v1 locker {locker_id} (relay-{relay_id}) did not come "
                        f"online before command"
                    ),
                )
                logger.info("v1 locker %s online; sending open", locker_id)

                command = gateway.send_command(locker_id, "open")
                command_id = command["id"]
                logger.info("open command enqueued (v1): id=%s", command_id)

                def open_failed() -> bool:
                    items = gateway.get_command_history(
                        locker_id=locker_id,
                        command_type="open",
                        status="failed",
                        limit=50,
                    )
                    return any(
                        item.get("command_id") == command_id for item in items
                    )

                wait_for(
                    open_failed,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"open command {command_id} did not reach failed "
                        f"on v1 relay-{relay_id} (NOT_SUPPORTED expected)"
                    ),
                )
                logger.info(
                    "command %s failed (NOT_SUPPORTED) end-to-end", command_id
                )

                # Sanity: команда НЕ помечена executed.
                executed = gateway.get_command_history(
                    locker_id=locker_id,
                    command_type="open",
                    status="executed",
                    limit=50,
                )
                assert not any(
                    item.get("command_id") == command_id for item in executed
                ), "v1 NOT_SUPPORTED command must not be executed"
                # v1 устройство не поддерживает LOCK_OPEN — дверь не открылась.
                assert runner.device.door != Door.OPEN
                logger.info("relay-%s door not opened on NOT_SUPPORTED", relay_id)
        finally:
            gateway.delete_locker(locker_id)

    def test_relay_multi_device_demux(self, gateway: GatewayClient):
        """Два шкафа A/B + два двойника: open→A, close→B, без cross-delivery.

        Проверяет, что backend публикует com/{relay_id} строго на свой топик и
        что каждый двойник применил ТОЛЬКО свою команду:
          A.door == OPEN, B.door == CLOSED (B открыли вручную до команды, чтобы
          close был наблюдаемым; команда open ушла только в A).
        """
        relay_a = _random_relay_id()
        relay_b = _random_relay_id()
        while relay_b == relay_a:
            relay_b = _random_relay_id()
        locker_a = _create_mqtt_locker(gateway, relay_a)["id"]
        locker_b = _create_mqtt_locker(gateway, relay_b)["id"]
        logger.info(
            "Created mqtt lockers A=%s (relay-%s) B=%s (relay-%s)",
            locker_a, relay_a, locker_b, relay_b,
        )

        try:
            with RelayRunner(relay_a) as run_a, RelayRunner(relay_b) as run_b:
                for lid, rid in ((locker_a, relay_a), (locker_b, relay_b)):
                    wait_for(
                        lambda lid=lid: gateway.get_connectivity(lid) == "online",
                        timeout=_ONLINE_TIMEOUT_S,
                        interval=2,
                        message=(
                            f"locker {lid} (relay-{rid}) did not come online"
                        ),
                    )

                # B открыта заранее → close даст наблюдаемый переход в CLOSED.
                run_b.device.door = Door.OPEN
                assert run_a.device.door == Door.CLOSED

                cmd_a = gateway.send_command(locker_a, "open")["id"]
                cmd_b = gateway.send_command(locker_b, "close")["id"]
                logger.info("enqueued open→A=%s close→B=%s", cmd_a, cmd_b)

                def executed(locker_id: str, ctype: str, cmd_id: str) -> bool:
                    items = gateway.get_command_history(
                        locker_id=locker_id, command_type=ctype,
                        status="executed", limit=50,
                    )
                    return any(it.get("command_id") == cmd_id for it in items)

                wait_for(
                    lambda: executed(locker_a, "open", cmd_a),
                    timeout=_COMMAND_TIMEOUT_S, interval=2,
                    message=f"open→A {cmd_a} not executed",
                )
                wait_for(
                    lambda: executed(locker_b, "close", cmd_b),
                    timeout=_COMMAND_TIMEOUT_S, interval=2,
                    message=f"close→B {cmd_b} not executed",
                )

                # Каждый двойник применил только свою команду — нет cross-delivery.
                wait_for(
                    lambda: run_a.device.door == Door.OPEN,
                    timeout=10, interval=0.5,
                    message="A did not open",
                )
                wait_for(
                    lambda: run_b.device.door == Door.CLOSED,
                    timeout=10, interval=0.5,
                    message="B did not close",
                )
                assert run_a.device.door == Door.OPEN
                assert run_b.device.door == Door.CLOSED
                logger.info(
                    "demux OK: A(relay-%s)=OPEN B(relay-%s)=CLOSED — no cross",
                    relay_a, relay_b,
                )
        finally:
            gateway.delete_locker(locker_a)
            gateway.delete_locker(locker_b)

    def test_relay_battery_signal_telemetry_to_history(
        self, gateway: GatewayClient
    ):
        """Heartbeat stat → battery/signal телеметрия долетает до history_api.

        Двойник с battery/signal и низким status_interval_s публикует stat
        периодически. Каждый stat ингестится locker_api → TelemetryProducer →
        outbound → history_api. Поллим telemetry API пока появятся показания
        battery И signal для шкафа. Требует поднятый history_api.
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        battery = 77
        signal = 19
        logger.info(
            "Created mqtt locker id=%s relay_id=%s (battery=%s signal=%s)",
            locker_id, relay_id, battery, signal,
        )

        try:
            with RelayRunner(
                relay_id,
                battery=battery,
                signal=signal,
                status_interval_s=2.0,
            ) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )
                assert runner.device.battery == battery

                def telemetry_present(code: str) -> bool:
                    items = gateway.get_telemetry(
                        locker_id=locker_id,
                        sensor_type_code=code,
                        limit=50,
                    )
                    return len(items) > 0

                wait_for(
                    lambda: telemetry_present("battery"),
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=3,
                    message=(
                        f"battery telemetry for {locker_id} did not reach "
                        f"history_api"
                    ),
                )
                wait_for(
                    lambda: telemetry_present("signal"),
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=3,
                    message=(
                        f"signal telemetry for {locker_id} did not reach "
                        f"history_api"
                    ),
                )
                logger.info(
                    "battery+signal telemetry for relay-%s landed in history",
                    relay_id,
                )
        finally:
            gateway.delete_locker(locker_id)

    def test_relay_goes_offline_after_silence(self, gateway: GatewayClient):
        """online → стоп двойника → connectivity флипается в offline.

        Двойник поднимается (online через stat), затем RelayRunner
        останавливается (устройство отключается / молчит). Offline-монитор
        locker_api помечает шкаф offline по протуханию last_seen_at.

        ВНИМАНИЕ: требует низкий MQTT_OFFLINE_TIMEOUT_S на locker_api для
        прогона (fast-mode backend/.env.fast: 40s/3s), иначе дефолт 350s.
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info(
            "Created mqtt locker id=%s relay_id=%s", locker_id, relay_id
        )

        try:
            with RelayRunner(relay_id) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )
                assert gateway.get_connectivity(locker_id) == "online"
                logger.info("relay-%s online; stopping to go silent", relay_id)
            # RelayRunner stopped here (context exited): device disconnected.

            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "offline",
                timeout=_OFFLINE_TIMEOUT_S,
                interval=2,
                message=(
                    f"locker {locker_id} (relay-{relay_id}) did not flip to "
                    f"offline after silence (is MQTT_OFFLINE_TIMEOUT_S low?)"
                ),
            )
            assert gateway.get_connectivity(locker_id) == "offline"
            logger.info("relay-%s flipped offline after silence", relay_id)
        finally:
            gateway.delete_locker(locker_id)

    def test_relay_reconnect_resubscribe(self, gateway: GatewayClient):
        """Reconnect: стоп двойника → offline → новый RelayRunner → снова online.

        Подтверждает, что новый процесс для того же relay_id переаутентифицируется
        на брокере (креды relay-{id} остаются провизионированными) и повторный
        stat возвращает шкаф online.
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info(
            "Created mqtt locker id=%s relay_id=%s", locker_id, relay_id
        )

        try:
            with RelayRunner(relay_id):
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online (1st)",
                )
                logger.info("relay-%s online (1st connect)", relay_id)

            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "offline",
                timeout=_OFFLINE_TIMEOUT_S,
                interval=2,
                message=f"locker {locker_id} did not flip offline before reconnect",
            )
            logger.info("relay-%s offline after 1st disconnect", relay_id)

            # Новый процесс для того же relay_id — должен переаутентифицироваться.
            with RelayRunner(relay_id):
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"locker {locker_id} did not come back online on "
                        f"reconnect"
                    ),
                )
                assert gateway.get_connectivity(locker_id) == "online"
                logger.info("relay-%s back online after reconnect", relay_id)
        finally:
            gateway.delete_locker(locker_id)

    # ── security_event + history (offline) ───────────────────────────────────
    #
    # Два пути рождения offline-события: молчание stat (offline-монитор) и
    # явный stat=offline (inline в ingest).

    def test_relay_offline_event_lands_in_history(self, gateway: GatewayClient):
        """online → молчание → offline security_event долетает в history_api.

        test_relay_goes_offline_after_silence проверяет лишь флип connectivity;
        здесь подтверждаем, что offline-монитор реально эмитит security_event и
        он оседает в history_api (event_type=offline, severity=medium,
        reason=heartbeat_timeout). Это «событие безопасности + запись в history»
        для релейного пути.

        Требует fast-mode MQTT_OFFLINE_TIMEOUT_S на locker_api
        (backend/.env.fast: 40s/3s), иначе дефолт 350s.
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]
        logger.info("Created mqtt locker id=%s relay_id=%s", locker_id, relay_id)

        try:
            with RelayRunner(relay_id):
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )
                logger.info("relay-%s online; stopping to go silent", relay_id)
            # RelayRunner stopped (context exited): device disconnected / silent.

            wait_for(
                lambda: gateway.get_connectivity(locker_id) == "offline",
                timeout=_OFFLINE_TIMEOUT_S,
                interval=2,
                message=(
                    f"locker {locker_id} (relay-{relay_id}) did not flip offline "
                    f"after silence (is MQTT_OFFLINE_TIMEOUT_S low?)"
                ),
            )

            wait_for(
                lambda: any(
                    e.get("event_type") == "offline"
                    and e.get("locker_id") == locker_id
                    for e in gateway.get_security_events(
                        locker_id=locker_id, event_type="offline",
                    )
                ),
                timeout=_EVENT_TIMEOUT_S,
                interval=2,
                message=(
                    f"offline security_event for {locker_id} did not reach "
                    f"history_api after the offline flip"
                ),
            )
            events = [
                e for e in gateway.get_security_events(
                    locker_id=locker_id, event_type="offline",
                )
                if e.get("event_type") == "offline"
            ]
            assert events, "offline event not found after wait"
            event = events[-1]
            assert event["severity"] == "medium", f"unexpected severity: {event}"
            metadata = event.get("metadata") or {}
            assert metadata.get("reason") == _OFFLINE_REASON_HEARTBEAT_TIMEOUT, (
                f"unexpected offline reason: {metadata}"
            )
            logger.info(
                "relay-%s offline security_event landed in history (reason=%s)",
                relay_id, metadata.get("reason"),
            )
        finally:
            gateway.delete_locker(locker_id)

    def test_relay_inline_offline_stat_lands_in_history(
        self, gateway: GatewayClient
    ):
        """stat со status=offline → немедленный offline security_event в history.

        Покрывает inline-ветку ingest._handle_stat (stat.status==OFFLINE): плата
        авторитетно сообщает offline одним stat — backend сразу флипает
        connectivity и эмитит offline-событие, не дожидаясь offline-монитора.
        Двойник остаётся подключённым; меняем только device.status и публикуем
        одиночный stat через RelayRunner.publish_stat().
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
                logger.info(
                    "relay-%s online; publishing stat status=offline", relay_id
                )

                # Плата сообщает о себе offline одним stat (не разрыв связи).
                runner.device.status = _RELAY_STATUS_OFFLINE
                runner.publish_stat()

                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "offline",
                    timeout=_EVENT_TIMEOUT_S,
                    interval=1,
                    message=(
                        f"locker {locker_id} did not flip offline from "
                        f"stat=offline"
                    ),
                )

                wait_for(
                    lambda: any(
                        e.get("event_type") == "offline"
                        and e.get("locker_id") == locker_id
                        for e in gateway.get_security_events(
                            locker_id=locker_id, event_type="offline",
                        )
                    ),
                    timeout=_EVENT_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"offline security_event for {locker_id} (inline stat=0) "
                        f"did not reach history_api"
                    ),
                )
                events = [
                    e for e in gateway.get_security_events(
                        locker_id=locker_id, event_type="offline",
                    )
                    if e.get("event_type") == "offline"
                ]
                assert events, "offline event not found after inline stat=offline"
                event = events[-1]
                assert event["severity"] == "medium", (
                    f"unexpected severity: {event}"
                )
                metadata = event.get("metadata") or {}
                assert metadata.get("reason") == _OFFLINE_REASON_HEARTBEAT_TIMEOUT, (
                    f"unexpected offline reason: {metadata}"
                )
                logger.info(
                    "relay-%s inline offline security_event landed in history",
                    relay_id,
                )
        finally:
            gateway.delete_locker(locker_id)

    def _run_lock_command(self, gateway: GatewayClient, runner: RelayRunner,
                          locker_id: str, command_type: str, door: int) -> None:
        command_id = gateway.send_command(locker_id, command_type)["id"]
        wait_for(
            lambda: any(
                item.get("command_id") == command_id
                for item in gateway.get_command_history(
                    locker_id=locker_id, command_type=command_type,
                    status="executed", limit=50,
                )
            ),
            timeout=_COMMAND_TIMEOUT_S,
            interval=2,
            message=f"{command_type} command {command_id} did not reach executed",
        )
        wait_for(
            lambda: runner.device.door == door,
            timeout=10,
            interval=0.5,
            message=f"device door did not follow {command_type}",
        )

    def test_relay_unauthorized_door_open_raises_intrusion(
        self, gateway: GatewayClient
    ):
        """Открытие по команде — не взлом; открытие при действующей «Закрыть» — взлом.

        Плата без камеры о несанкционированном открытии не сообщает (протокол
        §7.1), его распознаёт backend по stat: дверь закрыто→открыто, а
        последняя опубликованная в com/{id} команда замка — не OPEN.
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        def intrusions() -> list[dict]:
            return [
                e for e in gateway.get_security_events(
                    locker_id=locker_id, event_type="intrusion",
                )
                if e.get("event_type") == "intrusion"
            ]

        try:
            with RelayRunner(relay_id) as runner:
                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"locker {locker_id} did not come online",
                )
                self._run_lock_command(gateway, runner, locker_id, "open", Door.OPEN)
                self._run_lock_command(gateway, runner, locker_id, "close", Door.CLOSED)
                time.sleep(_INTRUSION_SETTLE_S)
                assert intrusions() == []

                runner.device.door = Door.OPEN
                runner.publish_stat()

                wait_for(
                    lambda: bool(intrusions()),
                    timeout=_EVENT_TIMEOUT_S,
                    interval=2,
                    message=f"intrusion for unauthorized open of {locker_id} not in history",
                )
                time.sleep(_INTRUSION_SETTLE_S)
                events = intrusions()
                assert len(events) == 1, events
                event = events[0]
                assert event["severity"] == "high", event
                descriptions = [v.get("description") for v in event["violated_thresholds"]]
                assert descriptions == [_UNAUTHORIZED_OPEN_DESCRIPTION], event
        finally:
            gateway.delete_locker(locker_id)
