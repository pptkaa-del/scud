from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from shared.database import AsyncPostgres
from shared.enums import CommandTransition, WsCloseCode, WsLifecycleEvent
from shared.metrics import record_ws_event

from ..mqtt.transport import DeviceTransport
from ..observability import command_metrics
from .command_expiry import DEFAULT_COMMAND_TTL_S, CommandExpiry

if TYPE_CHECKING:
    from ..locker_data.db.models.command import LockerCommand
    from ..locker_data.db.repositories.command import CommandRepository
    from .history_producers.command_history_producer import CommandHistoryProducer

logger = logging.getLogger(__name__)

# Причина терминального failed для команды, исчерпавшей retry-бюджет на
# ack-timeout (COR-15): устройство так и не подтвердило команду за отведённые
# попытки. Module-level константа — не зашиваем строковый литерал в бизнес-логику.
_ACK_TIMEOUT_FAILURE_REASON = "ack timeout, retries exhausted"

# Дефолтный интервал (с) поллинга БД-очереди команд. Был 30 — устройство,
# переподключившееся между тиками, ждало доставки команды до 30с. Реальный
# prod-override — env COMMAND_QUEUE_PROCESSOR_INTERVAL_S в main.py.
_DEFAULT_QUEUE_POLL_INTERVAL_S = 5


class CommandQueue:
    """
    Управляет очередью команд и отправкой их на устройства через транспорт.

    `_connections` хранит абстрактный `DeviceTransport` (WS или MQTT), а не
    конкретный WebSocketHandler — отправка делегируется транспорту.

    `_connections` защищён `asyncio.Lock`, чтобы исключить гонки при двух
    одновременных подключениях одного locker_id (новое соединение вытесняет
    старое и закрывает его, чтобы не оставлять зомби-сокетов).

    ОГРАНИЧЕНИЕ single-replica: `_connections` — это in-PROCESS реестр
    locker_id → транспорт (живой WS/MQTT-канал устройства). Команду из БД-очереди
    может доставить ТОЛЬКО та реплика, которая держит живой транспорт этого
    устройства. Поэтому `locker_api` обязан работать как ОДНА реплика (либо с
    sticky-маршрутизацией устройство → реплика). Cross-replica доставка
    (например, через Redis pub/sub) — задача на будущее. Остальные сервисы
    масштабируются свободно; путь outbox → history_api теперь multi-replica-safe
    (см. OutboundRepository.fetch_pending). Подробнее — DEPLOYMENT.md, раздел
    про масштабирование и ограничение single-replica.
    """

    def __init__(self, expiry: CommandExpiry | None = None) -> None:
        self._connections: dict[str, DeviceTransport] = {}  # locker_id -> device transport
        self._connections_lock = asyncio.Lock()
        self._running = False
        self._ack_timeout_running = False
        self._expiry = expiry

    @property
    def command_ttl_s(self) -> int:
        return self._expiry.ttl_s if self._expiry is not None else DEFAULT_COMMAND_TTL_S

    async def register_connection(self, locker_id: str, transport: DeviceTransport) -> None:
        """Зарегистрировать транспорт устройства.

        Если уже есть соединение для этого locker_id, оно закрывается, а его
        место занимает новое: после переподключения устройства старое соединение
        могло остаться "висеть" без TCP RST.
        """
        async with self._connections_lock:
            existing = self._connections.get(locker_id)
            if existing is not None and existing is not transport:
                logger.warning(
                    "Replacing existing connection for locker %s",
                    locker_id,
                )
                record_ws_event(WsLifecycleEvent.SUPERSESSION)
                close = getattr(existing, "close", None)
                if callable(close):
                    try:
                        close(code=WsCloseCode.SUPERSEDED, reason="superseded")
                    except Exception as exc:
                        logger.debug("Failed to close superseded connection: %s", exc)
            self._connections[locker_id] = transport
        logger.info(f"Registered connection for locker {locker_id}")

    async def unregister_connection(self, locker_id: str, transport: DeviceTransport | None = None) -> bool:
        """Удалить транспорт устройства.

        Если передан `transport`, удаление произойдёт только если он совпадает
        с зарегистрированным. Это защищает от ситуации, когда старое соединение
        в `on_close()` стирает запись свежего соединения, успевшего занять его место.

        Возвращает True, если запись действительно удалена. Возврат False
        означает, что текущая регистрация принадлежит другому транспорту
        (новое соединение перехватило locker_id) — в этом случае caller
        обычно должен пропустить дальнейшие cleanup-шаги вроде установки
        connectivity=OFFLINE, чтобы не сбить состояние новой сессии.
        """
        async with self._connections_lock:
            current = self._connections.get(locker_id)
            if current is None:
                return False
            if transport is not None and current is not transport:
                return False
            del self._connections[locker_id]
        logger.info(f"Unregistered connection for locker {locker_id}")
        return True

    def is_connected(self, locker_id: str) -> bool:
        """Проверить, подключено ли устройство.

        Чтение dict-а в Python атомарно, отдельный lock здесь не нужен —
        is_connected используется как best-effort проверка перед send_command.
        """
        return locker_id in self._connections

    def get_transport(self, locker_id: str) -> DeviceTransport | None:
        """Вернуть зарегистрированный транспорт (или None).

        Best-effort snapshot (dict.get атомарен): используется ingest'ом, чтобы
        понять, не зарегистрирован ли уже эквивалентный MqttTransport и можно ли
        пропустить повторную register_connection на heartbeat-stat'е.
        """
        return self._connections.get(locker_id)

    async def send_command(self, locker_id: str, command_data: dict[str, Any]) -> bool:
        """
        Отправить команду на устройство

        Returns:
            True если команда отправлена, False если устройство не подключено
        """
        # Snapshot под lock'ом, чтобы не получить TOCTOU race с supersession
        # из register_connection: между раздельным dict.get() и
        # send_command() новое подключение могло заместить транспорт и
        # закрыть старый (`code=4000 superseded`) — false negative (команда
        # не отправлена даже на свежий канал). Внутри lock'а только snapshot
        # ссылки; сам send_command выполняется ВНЕ lock'а (await в asyncio.Lock
        # корректен, но удерживать его на время сетевого write не нужно —
        # register/unregister должны иметь возможность пройти).
        async with self._connections_lock:
            transport = self._connections.get(locker_id)

        if not transport:
            logger.warning(f"Cannot send command: locker {locker_id} not connected")
            return False

        return await transport.send_command(command_data)

    async def prepare_send(self, command: Any, command_data: dict[str, Any]) -> None:
        """Делегировать транспорту pre-send подготовку command_data.

        Транспорт владеет любым шагом, зависящим от протокола (MQTT выделяет
        uint32 relay_cmd_id, WS — no-op). CommandQueue не знает о конкретном
        транспорте: и фоновый queue-processor, и inline-доставка в
        CommandListHandler зовут этот хелпер ПЕРЕД send_command. Если устройство
        не подключено — no-op (send_command затем вернёт False).
        """
        async with self._connections_lock:
            transport = self._connections.get(command.locker_id)
        if transport is None:
            return
        await transport.prepare_send(command, command_data)

    async def start_queue_processor(
        self,
        db: AsyncPostgres,
        check_interval: int = _DEFAULT_QUEUE_POLL_INTERVAL_S,
        history_producer: CommandHistoryProducer | None = None,
    ) -> None:
        """
        Запустить фоновый процесс обработки очереди команд

        Каждый тик: TTL-sweep (свой try — его сбой не блокирует доставку), выборка
        неистёкших pending, CLAIM-before-send (COR-02/18) и dispatch_claimed.

        Args:
            db: AsyncPostgres instance
            check_interval: Интервал проверки новых команд в секундах (default 5)
            history_producer: публикует 'sent' в command_history после доставки
        """
        self._running = True
        logger.info("Command queue processor started")

        from ..locker_data.db.repositories.command import CommandRepository

        while self._running:
            try:
                repo = CommandRepository(db)

                if self._expiry is not None:
                    try:
                        await self._expiry.sweep(repo)
                    except Exception:
                        logger.exception("Command TTL sweep failed")

                commands = await repo.get_pending_commands(ttl_s=self.command_ttl_s)

                for command in commands:
                    if not self.is_connected(command.locker_id):
                        logger.debug(
                            "Locker %s offline, command %s pending",
                            command.locker_id, command.id,
                        )
                        continue
                    claimed = await repo.update_status_to_sent(
                        command.id, ttl_s=self.command_ttl_s,
                    )
                    if claimed is None:
                        logger.debug(
                            "Command %s already claimed by another writer, "
                            "skipping send",
                            command.id,
                        )
                        continue
                    if not await dispatch_claimed(self, claimed, repo):
                        continue
                    logger.info(
                        "Command %s dispatched to locker %s",
                        claimed.id, claimed.locker_id,
                    )
                    if history_producer is None:
                        continue
                    try:
                        await history_producer.publish_sent(
                            command_id=claimed.id,
                            locker_id=claimed.locker_id,
                            command_type=claimed.type,
                            parameters=claimed.parameters,
                            created_by=claimed.created_by,
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to enqueue command_history sent for %s: %s",
                            claimed.id, exc,
                        )

                await asyncio.sleep(check_interval)

            except Exception as e:
                logger.error(f"Error in command queue processor: {e}", exc_info=True)
                await asyncio.sleep(check_interval)

        logger.info("Command queue processor stopped")

    def stop_queue_processor(self) -> None:
        """Остановить фоновый процесс обработки очереди"""
        self._running = False

    async def start_ack_timeout_loop(
        self,
        db: AsyncPostgres,
        interval_s: float = 60,
        timeout_min: int = 5,
        history_producer: Any | None = None,
    ) -> None:
        """Периодически обрабатывает команды, застрявшие в 'sent' дольше timeout_min.

        Sweep делит просроченные 'sent' команды на два потока (COR-15):

        * с ОСТАВШИМСЯ retry-бюджетом (retry_count + 1 < max_retries) →
          обратно в 'pending' (retry_count+1), переотправятся следующей
          итерацией queue processor (genuine re-attempt).
        * с ИСЧЕРПАННЫМ бюджетом (retry_count + 1 >= max_retries) → терминальный
          'failed'. БЕЗ этого зомби-команда висела бы в 'pending' навсегда:
          get_pending_commands фильтрует retry_count < max_retries и больше
          никогда её не подберёт, но и в failed её ничто не переводило.

        Для каждой failed-команды публикуется publish_failed в command_history
        (если передан history_producer), чтобы UI/история увидели терминальное
        событие, а не вечную queued-запись.

        Оба перехода claim-based (CAS-guard status='sent'): конкурентный
        device-ACK успел бы перевести строку в executed → она уже не sent → не
        клобберится этим sweep'ом.

        При дефолтном COMMAND_TTL_S (60 с < timeout_min) это лишь страховка:
        TTL-sweep переводит команду в failed раньше.

        Args:
            db: AsyncPostgres instance
            interval_s: Интервал между проверками в секундах
            timeout_min: Сколько минут команда должна провисеть в 'sent', чтобы её обработали
            history_producer: CommandHistoryProducer для публикации failed-событий
                (опционален: WS-only сборка без mqtt может оставить None — тогда
                команды всё равно переводятся в failed, просто без события истории)
        """
        from ..locker_data.db.repositories.command import CommandRepository

        self._ack_timeout_running = True
        logger.info(
            "ACK timeout monitor started (interval=%ss, timeout=%smin)",
            interval_s,
            timeout_min,
        )
        while self._ack_timeout_running:
            try:
                repo = CommandRepository(db)
                # 1) Не исчерпавшие бюджет → pending (retry_count+1, genuine
                #    re-attempt). CAS-guard status='sent' защищает executed/failed.
                reset = await repo.reset_timed_out_sent_commands(timeout_min)
                for _r in reset:
                    command_metrics.record_transition(
                        _r["type"], CommandTransition.ACK_TIMEOUT_RETRY,
                    )
                # 2) Исчерпавшие бюджет → терминальный failed; по каждой строке
                #    публикуем failed-событие истории (если есть producer).
                failed = await repo.fail_exhausted_sent_commands(
                    timeout_min, _ACK_TIMEOUT_FAILURE_REASON,
                )
                for command in failed:
                    logger.warning(
                        "Command %s on locker %s failed terminally: retries "
                        "exhausted after ack timeout",
                        command.id, command.locker_id,
                    )
                    command_metrics.record_transition(
                        command.type, CommandTransition.ACK_TIMEOUT_FAILED,
                    )
                    if history_producer is None:
                        continue
                    try:
                        await history_producer.publish_failed(
                            command_id=command.id,
                            locker_id=command.locker_id,
                            command_type=command.type,
                            error_message=_ACK_TIMEOUT_FAILURE_REASON,
                            parameters=command.parameters,
                            created_by=command.created_by,
                        )
                    except Exception as exc:
                        logger.error(
                            "Failed to enqueue command_history failed for %s: %s",
                            command.id, exc,
                        )
            except Exception:
                logger.exception("ACK timeout monitor query failed")
            await asyncio.sleep(interval_s)
        logger.info("ACK timeout monitor stopped")

    def stop_ack_timeout_loop(self) -> None:
        """Остановить фоновый процесс мониторинга ACK timeout"""
        self._ack_timeout_running = False


async def dispatch_claimed(
    queue: CommandQueue, command: LockerCommand, repo: CommandRepository,
) -> bool:
    """Отправить заявленную (sent) команду; при неудаче вернуть claim в pending.

    Свободная функция, а не метод: тесты хендлеров мокают CommandQueue через
    MagicMock(spec=...) и должны продолжать проверять prepare_send/send_command.
    """
    data: dict[str, Any] = {
        "type": "command",
        "command_id": command.id,
        "command_type": command.type,
        "parameters": command.parameters,
    }
    try:
        await queue.prepare_send(command, data)
        ok = await queue.send_command(command.locker_id, data)
    except Exception as exc:
        ok = False
        logger.warning(
            "Dispatch of command %s to locker %s raised: %s",
            command.id, command.locker_id, exc,
        )
    if ok:
        command_metrics.record_transition(command.type, CommandTransition.SENT)
        command_metrics.record_queue_latency(command.type, command.created_at, command.sent_at)
        return True
    command_metrics.record_transition(command.type, CommandTransition.SEND_FAILED)
    try:
        await repo.release_claim(command.id)
    except Exception:
        logger.exception("Failed to release claim of command %s", command.id)
    logger.warning(
        "Command %s not dispatched to locker %s; claim released for retry within TTL",
        command.id, command.locker_id,
    )
    return False
