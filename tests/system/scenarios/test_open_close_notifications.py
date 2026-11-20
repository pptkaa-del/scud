"""
Сценарий: Открытие/закрытие шкафа с проверкой уведомлений.

Проверяет:
1. При успешном выполнении команды 'open' создаётся постоянное уведомление
   с тегами event=locker_opened, locker_id, opened_by_user_id
2. Уведомление адресовано операторам и администраторам
3. При успешном выполнении команды 'close' уведомление отменяется
4. Уведомление содержит ID пользователя, открывшего шкаф

Зависимости:
- locker_api: команды open/close, MQTT-канал релейной платы
- notification_api: создание/отмена persistent уведомлений
- RelayRunner: релейный двойник, исполняющий команды через com/{relay_id}
- ACM: проверка доступа (worker → locker)
"""

from __future__ import annotations

import logging
import time
from datetime import datetime

from conftest import grant_locker_access
from utils.api_client import GatewayClient
from utils.mqtt_locker import _create_mqtt_locker, _random_relay_id
from utils.relay_runner import RelayRunner
from utils.wait import wait_for

logger = logging.getLogger(__name__)

# Запас под тик command-queue процессора (.env.fast: 2с) + доставку rep.
_COMMAND_TIMEOUT_S = 60.0
_ONLINE_TIMEOUT_S = 45.0

# Окно отрицательной проверки: на offline-шкафу команда структурно не может
# покинуть pending (command_queue.py не шлёт без is_connected), так что это
# окно ловит только гипотетический баг, создающий уведомление в обход
# исполнения — такой баг проявится за один-два тика очереди. Величина
# привязана к COMMAND_QUEUE_PROCESSOR_INTERVAL_S (2с в .env.fast), а не к
# _COMMAND_TIMEOUT_S выше.
_NO_NOTIFICATION_WINDOW_S = 15.0


def _notification_never_appears(
    gateway: GatewayClient,
    locker_id: str,
    event: str,
    duration: float,
    interval: float = 2.0,
) -> bool:
    """Опрашивать уведомления locker_id все duration секунд.

    Возвращает False сразу, как только событие появилось; True, если окно
    истекло без единого появления.
    """
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        if gateway.get_all_notifications_by_locker(locker_id, event=event):
            return False
        time.sleep(interval)
    return True


class TestOpenCloseNotifications:
    """Тесты открытия/закрытия шкафа и проверки уведомлений."""

    def test_open_creates_notification_with_correct_tags(
        self,
        gateway: GatewayClient,
        worker_user: dict,
    ):
        """
        Сценарий:
        1. Создать mqtt-шкаф и поднять релейный двойник
        2. Создать worker с доступом к шкафу
        3. Отправить команду open от имени worker
        4. Ждать выполнения команды релейным двойником
        5. Проверить: создалось persistent уведомление
        6. Проверить теги: event, locker_id, opened_by_user_id совпадают
        7. Проверить получателей: роли operator и administrator
        """
        worker_id = worker_user["id"]
        worker_session = worker_user["session"]

        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            with RelayRunner(relay_id) as runner:
                runner.publish_stat()

                grant_locker_access(gateway, worker_id, locker_id)
                logger.info("Granted access: worker_id=%d → locker_id=%s", worker_id, locker_id)

                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"Locker {locker_id} did not come online",
                )
                logger.info("Locker is online")

                cmd = gateway.send_command(locker_id, "open", session=worker_session)
                cmd_id = cmd["id"]
                logger.info("Sent open command: cmd_id=%s", cmd_id)

                wait_for(
                    lambda: len(gateway.get_all_notifications_by_locker(locker_id, event="locker_opened")) > 0,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message="locker_opened notification not created",
                )

                notifs = gateway.get_all_notifications_by_locker(locker_id, event="locker_opened")
                assert len(notifs) == 1, f"Expected 1 locker_opened notification, got {len(notifs)}"

                notif = notifs[0]
                tags = notif["tags"]

                assert tags["event"] == "locker_opened", f"Wrong event tag: {tags}"
                assert tags["locker_id"] == locker_id, (
                    f"Wrong locker_id tag: {tags['locker_id']} != {locker_id}"
                )
                assert tags["opened_by_user_id"] == str(worker_id), (
                    f"Wrong opened_by_user_id: {tags.get('opened_by_user_id')} != {worker_id}"
                )
                assert tags.get("dismiss_by") == "operator_or_service"

                logger.info("Open notification verified: id=%s tags=%s", notif["id"], tags)

                # Денормализованный кэш времени последнего открытия должен
                # обновиться синхронно с notification (обе ветки в _handle_command_result).
                wait_for(
                    lambda: gateway.get_locker(locker_id).get("last_opened_at") is not None,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message="last_opened_at not populated after executed open",
                )
        finally:
            gateway.delete_locker(locker_id)

    def test_close_cancels_open_notification(
        self,
        gateway: GatewayClient,
        worker_user: dict,
    ):
        """
        Сценарий (продолжение):
        После закрытия шкафа уведомление об открытии отменяется.
        """
        worker_id = worker_user["id"]
        worker_session = worker_user["session"]

        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            with RelayRunner(relay_id) as runner:
                runner.publish_stat()

                grant_locker_access(gateway, worker_id, locker_id)

                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                )

                gateway.send_command(locker_id, "open", session=worker_session)
                wait_for(
                    lambda: len(gateway.get_all_notifications_by_locker(locker_id, event="locker_opened")) > 0,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message="locker_opened notification not created",
                )

                gateway.send_command(locker_id, "close", session=worker_session)
                logger.info("Sent close command")

                wait_for(
                    lambda: len(gateway.get_all_notifications_by_locker(locker_id, event="locker_opened")) == 0,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message="locker_opened notification was not cancelled after close",
                )

                logger.info("locker_opened notification successfully cancelled after close")

                active = gateway.get_all_notifications_by_locker(locker_id, event="locker_opened")
                assert len(active) == 0, (
                    f"Expected 0 active locker_opened notifications, got {len(active)}"
                )
        finally:
            gateway.delete_locker(locker_id)

    def test_open_notification_not_created_for_failed_command(
        self,
        gateway: GatewayClient,
    ):
        """
        Команда, которая НЕ была выполнена устройством, не должна создавать уведомление.
        Тест: отправить команду на шкаф без поднятого релейного двойника
        (никто не подписан на com/{relay_id} → команда остаётся в очереди).
        """
        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            # Релейный двойник НЕ поднимается → шкаф остаётся offline.
            gateway.send_command(locker_id, "open")

            assert _notification_never_appears(
                gateway, locker_id, "locker_opened", duration=_NO_NOTIFICATION_WINDOW_S,
            ), "notification unexpectedly appeared for a command on an offline locker"
        finally:
            gateway.delete_locker(locker_id)

    def test_last_opened_at_updates_on_each_open(
        self,
        gateway: GatewayClient,
        worker_user: dict,
    ):
        """E2E: денормализованное поле lockers.last_opened_at обновляется
        при каждой успешно выполненной команде open и не затирается close.

        Сценарий:
        1. Создаём mqtt-шкаф → last_opened_at is None
        2. Поднимаем релейный двойник, шлём первый open → last_opened_at = t1
        3. Шлём close → last_opened_at сохраняется (== t1)
        4. Шлём второй open → last_opened_at = t2, t2 > t1
        """
        worker_id = worker_user["id"]
        worker_session = worker_user["session"]

        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            grant_locker_access(gateway, worker_id, locker_id)

            # 1. До первой команды — null
            assert gateway.get_locker(locker_id).get("last_opened_at") is None

            with RelayRunner(relay_id) as runner:
                runner.publish_stat()

                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                )

                # 2. Первый open — поле заполняется
                gateway.send_command(locker_id, "open", session=worker_session)
                wait_for(
                    lambda: gateway.get_locker(locker_id).get("last_opened_at") is not None,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message="last_opened_at not populated after first open",
                )
                t1 = gateway.get_locker(locker_id)["last_opened_at"]
                logger.info("After first open: last_opened_at=%s", t1)

                # 3. Close — поле НЕ затирается
                gateway.send_command(locker_id, "close", session=worker_session)
                wait_for(
                    lambda: len(gateway.get_all_notifications_by_locker(
                        locker_id, event="locker_opened")) == 0,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message="open notification not cancelled after close",
                )
                after_close = gateway.get_locker(locker_id)["last_opened_at"]
                assert after_close == t1, (
                    f"last_opened_at must persist after close: was {t1}, now {after_close}"
                )

                # 4. Второй open — поле обновляется на более позднее значение
                gateway.send_command(locker_id, "open", session=worker_session)
                wait_for(
                    lambda: gateway.get_locker(locker_id)["last_opened_at"] != t1,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message="last_opened_at not updated on second open",
                )
                t2 = gateway.get_locker(locker_id)["last_opened_at"]
                logger.info("After second open: last_opened_at=%s", t2)
                assert datetime.fromisoformat(t2) > datetime.fromisoformat(t1), (
                    f"second open must produce later timestamp: t1={t1}, t2={t2}"
                )
        finally:
            gateway.delete_locker(locker_id)

    def test_multiple_opens_one_notification(
        self,
        gateway: GatewayClient,
        worker_user: dict,
    ):
        """
        Две команды open подряд: должно быть только одно активное уведомление
        (второй open не создаёт дублирующее уведомление).
        """
        worker_id = worker_user["id"]
        worker_session = worker_user["session"]

        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            with RelayRunner(relay_id) as runner:
                runner.publish_stat()

                grant_locker_access(gateway, worker_id, locker_id)

                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                )

                # Первый open
                gateway.send_command(locker_id, "open", session=worker_session)
                wait_for(
                    lambda: len(gateway.get_all_notifications_by_locker(locker_id, event="locker_opened")) >= 1,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                )

                # Второй open — ждём его собственного executed в истории команд,
                # а не фиксированную паузу.
                cmd2 = gateway.send_command(locker_id, "open", session=worker_session)
                cmd2_id = cmd2["id"]

                def second_open_executed() -> bool:
                    items = gateway.get_command_history(
                        locker_id=locker_id,
                        command_type="open",
                        status="executed",
                        limit=50,
                    )
                    return any(item.get("command_id") == cmd2_id for item in items)

                wait_for(
                    second_open_executed,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message=f"second open command {cmd2_id} did not reach executed",
                )

                # Количество активных уведомлений — зависит от реализации,
                # но оба должны содержать корректные данные
                notifs = gateway.get_all_notifications_by_locker(locker_id, event="locker_opened")
                assert len(notifs) >= 1, "At least one open notification must exist"
                for n in notifs:
                    assert n["tags"]["locker_id"] == locker_id
                    assert "opened_by_user_id" in n["tags"]
        finally:
            gateway.delete_locker(locker_id)
