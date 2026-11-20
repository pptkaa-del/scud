"""Demux + handle incoming relay MQTT messages (stat/rep/photos). Reuses
CommandQueue and the command-result path; updates locker connectivity/state."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
import itertools
import logging
import os
import time
from typing import Any
import uuid

from shared.enums import RelayMessageOutcome, RelayPhotoMetaKey, SecurityEventType
from shared.metrics import record_relay_message, record_relay_photo_frame
from shared.mqtt_client import MqttQoS
from shared.utils.rate_limit import TokenBucket

from ..locker_data.db.models.locker import Connectivity, DoorState, OperationalStatus
from ..locker_data.db.models.relay import RELAY_CHANNELS, RelayState
from ..locker_data.db.models.relay_photo_series import RelayPhotoSeries
from ..locker_data.db.models.sensor_type import SensorCode
from ..services.history_producers.offline_producer import (
    REASON_HEARTBEAT_TIMEOUT,
)
from ..services.history_producers.security_event_producer import SENSOR_TYPE_INTRUSION
from ..services.history_producers.telemetry_producer import make_reading
from ..services.media_buffer import MediaTransferMeta, MediaTransferResult
from . import relay_codec as rc
from .photo_series import (
    FrameDisposition,
    classify_frame,
    frame_digest,
    frame_media_uid,
    frame_metadata,
    series_intrusion_uid,
    validate_frame,
)
from .relay_codec import encode_com
from .relay_protocol import (
    TOPIC_REP,
    TOPIC_STAT,
    Command,
    DeviceStatus,
    DoorStateRaw,
    PowerSource,
    ProtocolVersion,
    parse_device_topic,
    topic_com,
)
from .results import DEVICE_REJECTION_REASONS, NOTIF_DISMISS_BY, NOTIF_ROLES, locker_display
from .transport import MqttTransport, RelayCmdIdAllocator

logger = logging.getLogger(__name__)

# Раздельные коды sensor_type для телеметрии релейной платы (seed в 001_init.sql).
_SENSOR_BATTERY = SensorCode.BATTERY
_SENSOR_SIGNAL = SensorCode.SIGNAL
_SENSOR_DOOR = SensorCode.DOOR
_SENSOR_POWER = SensorCode.POWER
_SENSOR_TAMPER = SensorCode.TAMPER
_SENSOR_NET_TYPE = SensorCode.NET_TYPE
_SENSOR_ERROR = SensorCode.ERROR
# Серия фото платы — снимок intrusion-события. Код переиспользуем из
# security_event_producer: producer_for_sensor_type сравнивает ИМЕННО с ним,
# своя копия строки молча увела бы событие в threshold_violation.
_SENSOR_INTRUSION = SENSOR_TYPE_INTRUSION

_PHOTO_MIME = "image/jpeg"
_PHOTO_KIND = "photo"
_INTRUSION_VIOLATION = "Плата прислала серию фотографий вторжения"
_UNAUTHORIZED_OPEN_VIOLATION = "Дверь открыта без команды «Открыть»"
# Серия без поставленного intrusion старше этого возраста подхватывается sweep'ом.
_PENDING_INTRUSION_AGE_S = 60.0
_PENDING_INTRUSION_BATCH = 100
_BUCKETS_MAX = 2048
_RELAY_STATE_VALUES = frozenset(int(s) for s in RelayState)

# Маппинг сырого door-состояния платы (v2) в доменное DoorState шкафа.
_DOOR_MAP = {
    DoorStateRaw.OPEN: DoorState.OPEN,
    DoorStateRaw.CLOSED: DoorState.CLOSED,
}


class RelayIngest:
    def __init__(self, repo: Any, command_queue: Any, command_results: Any,
                 mqtt_client: Any, telemetry: Any = None,
                 offline_producer: Any = None, sensor_repo: Any = None,
                 allocator: RelayCmdIdAllocator | None = None,
                 security_producer_factory: Any = None,
                 media_producer: Any = None,
                 photo_series_repo: Any = None,
                 photo_series_epoch_s: float = 86400.0,
                 photo_series_max_bytes: int = rc.MAX_RELAY_PHOTO_BYTES,
                 photo_series_rate: float = 0.1,
                 photo_series_burst: float = 10.0,
                 telemetry_rate: float = 1.0,
                 telemetry_burst: float = 5.0,
                 command_repo: Any = None,
                 notification_client: Any = None,
                 relay_repo: Any = None) -> None:
        self._repo = repo
        self._relays = relay_repo
        self._relay_locks: dict[str, asyncio.Lock] = {}
        self._relay_applied: dict[str, int] = {}
        self._board_info: dict[str, tuple[int, bool]] = {}
        self._relay_channels_ready: set[str] = set()
        self._relay_states: dict[str, dict[int, int]] = {}
        self._queue = command_queue
        self._results = command_results
        self._mqtt = mqtt_client
        # Аллокатор relay_cmd_id (узкий порт RelayCmdIdAllocator; CommandRepository
        # удовлетворяет ему структурно) прокидывается в каждый построенный
        # MqttTransport, чтобы транспорт сам выделял relay_cmd_id в prepare_send
        # (CommandQueue больше не знает об mqtt-специфике). None в части unit-тестов.
        self._allocator = allocator
        self._telemetry = telemetry
        # OfflineEventProducer: эмитит offline history event при inline stat=0.
        # None в части unit-тестов — тогда событие просто не пишется.
        self._offline = offline_producer
        # SensorRepository для ленивого resolve/create battery|signal датчиков
        # релейного локера: телеметрия платы приходит без sensor_id, history_api
        # требует UUID. None в части unit-тестов — тогда телеметрия не пишется.
        self._sensors = sensor_repo
        # Кэш (locker_id, sensor_type_code) -> sensor_id UUID. Резолв идемпотентен,
        # но кэш убирает лишний SELECT на каждый heartbeat-stat.
        self._sensor_id_cache: dict[tuple[str, str], str] = {}
        # Фабрика security-event продьюсеров: код датчика → продьюсер.
        # device-asserted события (alarm, power-loss, ERROR) не проходят через
        # StateMonitor — они эмитятся напрямую здесь. None = нет продьюсера
        # (unit-тесты без wiring или явно отключено).
        self._security_factory = security_producer_factory
        # MediaProducer + реестр серий (RelayPhotoSeriesRepository): каждый кадр
        # photos публикуется сразу. Без любого из них кадр только учитывается.
        self._media = media_producer
        self._series_repo = photo_series_repo
        self._series_epoch_s = photo_series_epoch_s
        self._series_max_bytes = photo_series_max_bytes
        self._series_rate = photo_series_rate
        self._series_burst = photo_series_burst
        # Лимит на новые серии per relay: сбойная/поддельная плата не зальёт
        # реестр и outbox сериями с новыми event_id.
        self._series_buckets: OrderedDict[int, TokenBucket] = OrderedDict()
        self._series_claims = _KeyedLocks()
        # Последнее наблюдённое PowerSource на relay_id. Нужно для определения
        # перехода MAINS→BATTERY (только однократная эмиссия события на переход).
        self._last_power: dict[str, PowerSource] = {}
        # Edge-trigger для alarm-бита (v1): эмитим tamper только на 0→1.
        # Отсутствие записи = 0 (не было alarm), паритет с _last_power.
        self._last_alarm: dict[str, int] = {}
        # Edge-trigger для статуса ERROR: True = последний known статус был ERROR.
        # Паритет с _last_power/alarm — повторные ERROR-heartbeat'ы не плодят дубли.
        self._last_status_error: dict[str, bool] = {}
        # Последнее значение, успешно записанное в датчик ошибки платы. Отдельно от
        # _last_status_error: тот управляет событием, этот — записью (с ретраем
        # при неудачном резолве; отсутствие ключа = надо записать).
        self._error_sensor_value: dict[str, bool] = {}
        # gmqtt обрабатывает сообщения конкурентно: без лока кэш и БД расходятся
        # при перестановке завершения записей, и edge-проверка залипает навсегда.
        self._error_sensor_locks: dict[str, asyncio.Lock] = {}
        # Без command_repo несанкционированное открытие не распознаётся.
        self._commands = command_repo
        self._notifier = notification_client
        # Лок держится от перехода двери до записи «Взлом»: иначе медленная
        # проверка команды для open перетрёт False, записанный следующим close.
        self._door_locks: dict[str, asyncio.Lock] = {}
        # Сообщения обрабатываются конкурентно: stat, пришедший раньше уже
        # применённого, дверь не трогает, иначе она залипнет в старом состоянии.
        # Порядок — номер, выданный до первого await, а не часы.
        self._arrival = itertools.count()
        self._door_applied: dict[str, int] = {}
        self._intrusion_value: dict[str, bool] = {}
        # op_status кэш с TTL: get_operational_status — лишний SELECT на каждый
        # heartbeat; статус меняется редко, поэтому кэшируем на короткий TTL.
        self._op_status_cache: dict[str, tuple[OperationalStatus | None, float]] = {}
        self._op_status_ttl_s = float(os.getenv("RELAY_OP_STATUS_TTL_S", "30"))
        # Per-device TokenBucket для rate-limit телеметрии. OrderedDict обеспечивает
        # LRU: при превышении лимита вытесняется самый давний (least-recently-used)
        # девайс. Ключ — int device_id (relay_id).
        self._telemetry_rate = telemetry_rate
        self._telemetry_burst = telemetry_burst
        self._telemetry_buckets: OrderedDict[int, TokenBucket] = OrderedDict()

    async def handle_message(
        self, topic: str, payload: bytes, received_at: datetime | None = None,
    ) -> None:
        parsed = parse_device_topic(topic)
        if parsed is None:
            # $SYS и всё, что устройство публиковать не может: не путь релейной
            # платы, поэтому счётчик исходов намеренно не трогаем.
            logger.debug("ignoring message on topic %s", topic)
            return
        kind, topic_relay_id = parsed
        arrival = next(self._arrival)
        try:
            if topic_relay_id is not None:
                self._reject_id_mismatch(payload, topic_relay_id)
            if kind == TOPIC_STAT:
                await self._handle_stat(payload, received_at or datetime.now(UTC), arrival)
            elif kind == TOPIC_REP:
                await self._handle_rep(payload, received_at or datetime.now(UTC))
            else:
                await self._handle_photo(payload)
        except (ValueError, KeyError) as exc:
            # Malformed/forged device frame (bad length, unknown enum value, etc.).
            # Log length only — NEVER the payload bytes — and drop. Do not swallow
            # unexpected errors: let anything that isn't a decode error propagate.
            logger.warning("dropping malformed relay frame on %s (%d bytes): %s",
                           topic, len(payload), exc)
            record_relay_message(RelayMessageOutcome.DROPPED_MALFORMED)

    @staticmethod
    def _reject_id_mismatch(payload: bytes, topic_relay_id: int) -> None:
        """Сверяет id из per-id топика с id внутри кадра.

        У stat/rep/photos device_id — первое поле (uint32 LE), поэтому проверка
        одна на все три. Расхождение означает публикацию от чужого имени:
        единственный способ это поймать, пока платам разрешён общий топик.
        """
        if len(payload) < 4:
            raise ValueError("frame too short to carry device_id")
        frame_relay_id = int.from_bytes(payload[:4], "little")
        if frame_relay_id != topic_relay_id:
            raise ValueError(
                f"topic relay_id {topic_relay_id} != frame relay_id {frame_relay_id}")

    async def _resolve(self, device_id: int) -> str | None:
        locker_id: str | None = await self._repo.get_locker_id_by_relay_id(device_id)
        if locker_id is None:
            logger.warning("stat/rep from unknown relay_id=%s", device_id)
            record_relay_message(RelayMessageOutcome.DROPPED_UNKNOWN_RELAY)
        return locker_id

    def _is_equivalent_transport(self, locker_id: str, relay_id: int) -> bool:
        """True, если уже зарегистрирован MqttTransport того же relay_id.

        Позволяет heartbeat-stat'ам обновлять connectivity без повторной
        register_connection (которая логировала бы WARNING "Replacing existing
        connection" на КАЖДЫЙ stat и зря дёргала бы supersession-close).
        """
        getter = getattr(self._queue, "get_transport", None)
        if not callable(getter):
            return False
        current = getter(locker_id)
        return (isinstance(current, MqttTransport)
                and current.relay_id == relay_id)

    async def _handle_stat(self, payload: bytes, received_at: datetime,
                           arrival: int) -> None:
        stat = rc.decode_stat(payload)
        locker_id = await self._resolve(stat.device_id)
        if locker_id is None:
            return
        # stat field explicitly offline → пометить connectivity=offline, снять
        # транспорт и эмитить offline history event. Плата авторитетно
        # сообщает о себе offline, поэтому teardown маршрута здесь корректен и
        # обязателен: offline-монитор матчит только connectivity='online', так что
        # шкаф, флипнутый в offline через stat=0, монитор уже не подберёт — без
        # unregister здесь MqttTransport утёк бы в CommandQueue навсегда, а inline
        # dispatch продолжал бы слать команды на мёртвую плату. Stale-retained/
        # forged stat=0 порвёт живой маршрут лишь до следующего stat реальной
        # платы (он же re-register), forged-запись в shared topic — документированное
        # ограничение Phase-1 ACL. Делаем это ТОЛЬКО на переходе online→offline:
        # повторный/retained stat=0 на уже offline-шкафе не должен плодить
        # дубликаты offline-события и лишний unregister (идемпотентность).
        if stat.status == DeviceStatus.OFFLINE:
            # Атомарный online→offline флип (COR-19): один UPDATE … WHERE
            # connectivity='online' RETURNING. Без этого read-get_connectivity +
            # write-update_connectivity TOCTOU-гонят: два interleaved stat=0
            # (retained + live) оба наблюдали бы online и оба эмитили offline-
            # событие + дважды unregister. mark_offline_if_online возвращает True
            # ровно одному вызову — он и снимает транспорт + эмитит событие;
            # проигравший (0 строк) — no-op (идемпотентность сохранена).
            if not await self._repo.mark_offline_if_online(locker_id):
                record_relay_message(RelayMessageOutcome.PROCESSED)
                return
            await self._queue.unregister_connection(locker_id)
            await self._emit_offline(locker_id)
            record_relay_message(RelayMessageOutcome.PROCESSED)
            return
        # Register transport только если ещё не подключён ИЛИ зарегистрирован не
        # эквивалентный MqttTransport — иначе heartbeat-stat'ы порождали бы
        # "Replacing existing connection" WARNING на каждый stat. В любом случае
        # update_connectivity(ONLINE) обновляет last_seen_at (offline-монитор
        # зависит от него).
        if not (self._queue.is_connected(locker_id)
                and self._is_equivalent_transport(locker_id, stat.device_id)):
            transport = MqttTransport(
                locker_id=locker_id, relay_id=stat.device_id, client=self._mqtt,
                allocator=self._allocator)
            await self._queue.register_connection(locker_id, transport)
        await self._repo.update_connectivity(locker_id, Connectivity.ONLINE)

        # Device-asserted events: emit security events directly (no threshold
        # dependency), in order: ERROR, alarm (tamper), power-source transition.

        # status=ERROR (A5) — edge-trigger: эмитим событие только на переходе в ERROR,
        # повторные ERROR-heartbeat'ы не плодят дубли (паритет с power/alarm).
        # Датчик ошибки пишется на каждом изменении ДО гейтов MAINTENANCE и
        # rate-limit: это состояние платы, а не поток телеметрии.
        state_readings: list[dict[str, Any]] = []
        if stat.status is not None:
            is_error = stat.status == DeviceStatus.ERROR
            if is_error and not self._last_status_error.get(locker_id, False):
                logger.warning("relay %s reported ERROR status for locker %s",
                               stat.device_id, locker_id)
                await self._emit_security(
                    locker_id, _SENSOR_ERROR, ["Устройство сообщило об ошибке (статус ERROR)"])
            self._last_status_error[locker_id] = is_error
            if self._sensors is not None:
                lock = self._error_sensor_locks.setdefault(locker_id, asyncio.Lock())
                async with lock:
                    if (self._error_sensor_value.get(locker_id) != is_error
                            and await self._append_reading(
                                state_readings, locker_id, _SENSOR_ERROR, is_error)):
                        self._error_sensor_value[locker_id] = is_error

        # alarm (A3) — v1-only поле; edge-trigger (0→1), чтобы залипший датчик не
        # заливал outbox security-событием на каждый heartbeat (паритет с power).
        if stat.alarm is not None:
            if self._last_alarm.get(locker_id, 0) == 0 and stat.alarm == 1:
                await self._emit_security(
                    locker_id, _SENSOR_TAMPER, ["Сработал датчик вскрытия корпуса (тревога)"])
            self._last_alarm[locker_id] = stat.alarm

        # power transition (A4): MAINS→BATTERY emits a single power-loss event.
        if stat.power is not None:
            prev = self._last_power.get(locker_id)
            if prev == PowerSource.MAINS and stat.power == PowerSource.BATTERY:
                await self._emit_security(
                    locker_id, _SENSOR_POWER,
                    ["Питание переключено на резервную батарею (пропало основное питание)"])
            self._last_power[locker_id] = stat.power

        # Door state from a v2 stat syncs lockers.door via the TELEMETRY path
        # (sync_door_state), NOT the command path (update_door). Критично: stat —
        # это периодический heartbeat, повторяющий залипшее состояние; командный
        # update_door(OPEN) на каждый такой stat сбрасывал бы last_opened_at и
        # затирал who_open_last (баг «переоткрытие каждые 5 минут»). sync_door_state
        # трогает только door и edge-triggered.
        if stat.door is not None:
            door = _DOOR_MAP.get(stat.door)
            if door is not None:
                await self._sync_door(locker_id, door, state_readings, received_at, arrival)

        await self._sync_relays(locker_id, stat, arrival)

        # Battery/signal telemetry → history_api via the existing TelemetryProducer.
        # sensor_id ДОЛЖЕН быть реальным UUID (history_api.sensor_telemetry.sensor_id —
        # UUID NOT NULL): резолвим/создаём датчик по (locker_id, type_code), как
        # WS-путь берёт sensor_id из самого сообщения. Синтетическую строку
        # "<relay_id>:battery" не шлём — она роняла insert в history с 500.
        if self._telemetry is not None and self._sensors is not None:
            readings = state_readings
            if await self._telemetry_allowed(locker_id, stat.device_id):
                if stat.battery is not None:
                    await self._append_reading(readings, locker_id, _SENSOR_BATTERY, stat.battery)
                if stat.signal is not None:
                    await self._append_reading(readings, locker_id, _SENSOR_SIGNAL, stat.signal)
                # Значение датчика двери уже записал _sync_door.
                if stat.door is not None:
                    await self._append_reading(
                        readings, locker_id, _SENSOR_DOOR, stat.door == DoorStateRaw.OPEN,
                        persist=False)
                if stat.power is not None:
                    await self._append_reading(
                        readings, locker_id, _SENSOR_POWER, int(stat.power))
                # Сырой int без IntEnum: неизвестный тип канала не должен ронять кадр.
                if stat.net_type is not None:
                    await self._append_reading(
                        readings, locker_id, _SENSOR_NET_TYPE, stat.net_type)
            if readings:
                await self._telemetry.publish_batch(locker_id, readings)
        record_relay_message(RelayMessageOutcome.PROCESSED)

    async def _sync_door(self, locker_id: str, door: DoorState,
                         readings: list[dict[str, Any]], received_at: datetime,
                         arrival: int) -> None:
        """Синхронизировать дверь и распознать несанкционированное открытие.

        Условие протокола платы §7.1: прежнее зафиксированное состояние двери —
        закрыто, текущее — открыто, а действующая команда замка не «Открыть».
        Плата сообщает о нём только серией фото и только при наличии камеры,
        поэтому сервер проверяет его сам. Прежнее состояние берётся из датчика
        door, который пишет только stat: lockers.door обновляет и командный
        путь, по нему переход ложно срабатывал бы после CLOSE при открытой двери.
        """
        lock = self._door_locks.setdefault(locker_id, asyncio.Lock())
        async with lock:
            if arrival < self._door_applied.get(locker_id, -1):
                logger.debug("stale door stat skipped for locker %s", locker_id)
                return
            self._door_applied[locker_id] = arrival
            await self._repo.sync_door_state(locker_id, door)
            if self._sensors is None:
                return
            door_sensor = await self._resolve_sensor_id(locker_id, _SENSOR_DOOR)
            if door_sensor is None:
                return
            is_open = door is DoorState.OPEN
            changed, previous = await self._sensors.swap_value(door_sensor, is_open)
            if not is_open:
                if await self._intrusion_active(locker_id):
                    await self._set_intrusion(locker_id, False, readings)
                return
            if (not changed or previous is not False or self._commands is None
                    or await self._open_authorized(locker_id, received_at)):
                return
            logger.warning("unauthorized door open on locker %s", locker_id)
            await self._emit_security(
                locker_id, _SENSOR_INTRUSION, [_UNAUTHORIZED_OPEN_VIOLATION])
            await self._set_intrusion(locker_id, True, readings)
            await self._raise_intrusion_alarm(locker_id)

    async def _sync_relays(self, locker_id: str, stat: rc.Stat, arrival: int) -> None:
        """board_version/has_relays шкафа и состояния r1..r4 в locker_relays.

        Пишется только изменившееся; кадр, пришедший раньше применённого,
        пропускается (как у двери). Ошибка БД здесь не роняет приём stat.
        """
        if stat.version == ProtocolVersion.V1:
            has_relays = True
        elif stat.version == ProtocolVersion.V2:
            has_relays = stat.relay_en == 1
        else:
            return
        lock = self._relay_locks.setdefault(locker_id, asyncio.Lock())
        async with lock:
            if arrival < self._relay_applied.get(locker_id, -1):
                logger.debug("stale relay stat skipped for locker %s", locker_id)
                return
            self._relay_applied[locker_id] = arrival
            try:
                board = (stat.version, has_relays)
                if self._board_info.get(locker_id) != board:
                    await self._repo.update_board_info(locker_id, stat.version, has_relays)
                    self._board_info[locker_id] = board
                if not has_relays or stat.relays is None or self._relays is None:
                    return
                states = self._valid_relay_states(locker_id, stat.relays)
                if locker_id not in self._relay_channels_ready:
                    await self._relays.ensure_channels(locker_id)
                    self._relay_channels_ready.add(locker_id)
                if states and self._relay_states.get(locker_id) != states:
                    await self._relays.set_reported(locker_id, states)
                    self._relay_states[locker_id] = states
            except Exception:
                logger.exception("relay state sync failed for locker %s", locker_id)

    @staticmethod
    def _valid_relay_states(locker_id: str, raw: list[int]) -> dict[int, int]:
        states: dict[int, int] = {}
        for channel, value in zip(RELAY_CHANNELS, raw, strict=False):
            if value in _RELAY_STATE_VALUES:
                states[channel] = value
            else:
                logger.warning("relay r%d=%d out of range on locker %s",
                               channel, value, locker_id)
        return states

    async def _raise_intrusion_alarm(self, locker_id: str) -> None:
        """Тревога шкафа и уведомление операторам, как у StateMonitor. Не бросает.

        Уведомление — только на переходе NORMAL→ALARM: шкаф на обслуживании
        тревогу не поднимает, а уже тревожный ждёт сброса оператором и
        повторные вскрытия только пишет в журнал событий.
        """
        try:
            locker = await self._repo.raise_alarm(locker_id)
            if locker is None:
                return
            logger.warning("Locker %s entered ALARM state: unauthorized door open", locker_id)
            if self._notifier is None:
                return
            await self._notifier.create_persistent(
                text=(f"Взлом: шкаф {locker_display(locker, locker_id)} открыт без "
                      f"команды «Открыть». Шкаф переведён в режим тревоги."),
                tags={
                    "event": SecurityEventType.INTRUSION.value,
                    "locker_id": locker_id,
                    "dismiss_by": NOTIF_DISMISS_BY,
                },
                roles=NOTIF_ROLES,
            )
        except Exception:
            logger.exception("Failed to raise intrusion alarm for locker %s", locker_id)

    async def _open_authorized(self, locker_id: str, at: datetime) -> bool:
        """Ошибка проверки = тревога: ложное срабатывание дешевле пропущенного взлома."""
        try:
            return bool(await self._commands.is_open_authorized(
                locker_id, at, DEVICE_REJECTION_REASONS))
        except Exception:
            logger.exception("lock command lookup failed for locker %s; "
                             "treating door open as unauthorized", locker_id)
            return False

    async def _intrusion_active(self, locker_id: str) -> bool:
        cached = self._intrusion_value.get(locker_id)
        if cached is not None:
            return cached
        # После рестарта кэша нет: сбрасываем датчик, только если он уже заведён.
        existing = await self._sensors.get_id_by_locker_and_type_code(
            locker_id, _SENSOR_INTRUSION)
        if existing is None:
            self._intrusion_value[locker_id] = False
        return existing is not None

    async def _set_intrusion(self, locker_id: str, value: bool,
                             readings: list[dict[str, Any]]) -> None:
        if await self._append_reading(readings, locker_id, _SENSOR_INTRUSION, value):
            self._intrusion_value[locker_id] = value

    async def _telemetry_allowed(self, locker_id: str, device_id: int) -> bool:
        """Гейты периодической телеметрии stat'а.

        A6: в MAINTENANCE телеметрия подавляется — шкаф обслуживается, данные не
        нужны; connectivity/security-события к этому моменту уже обработаны.
        Проверка идёт до bucket'а, чтобы MAINTENANCE не тратил токены.
        A7: per-device rate-limit — слишком частые stat'ы не пишут телеметрию.
        """
        if await self._op_status(locker_id) == OperationalStatus.MAINTENANCE:
            return False
        return self._telemetry_bucket(device_id).try_consume()

    async def _append_reading(self, readings: list[dict[str, Any]], locker_id: str,
                              sensor_type_code: str, value: Any, *,
                              persist: bool = True) -> bool:
        """Добавить reading с разрезолвленным UUID sensor_id (или пропустить).

        Резолв кэшируется per (locker_id, code). Если датчик нельзя создать
        (нет seed-типа) — reading пропускается с WARNING, телеметрия для него
        просто не попадёт в history (не валим остальной батч).

        Returns:
            True, если значение записано и reading добавлен."""
        sensor_id = await self._resolve_sensor_id(locker_id, sensor_type_code)
        if sensor_id is None:
            logger.warning("no sensor_id for %s on locker %s — skipping reading",
                           sensor_type_code, locker_id)
            return False
        # WS-паритет (_telemetry_mixin.py:139): current_value нужен StateMonitor'у
        # для порогов. Без этого MQTT-локеры никогда не триггерят threshold-алерты.
        if persist:
            await self._sensors.update_value(sensor_id, value)
        readings.append(make_reading(sensor_id, sensor_type_code, value))
        return True

    async def _resolve_sensor_id(self, locker_id: str, sensor_type_code: str) -> str | None:
        """UUID датчика (locker_id, code), создавая его при первом stat'е. Кэшируется."""
        key = (locker_id, sensor_type_code)
        cached = self._sensor_id_cache.get(key)
        if cached is not None:
            return cached
        sensor_id: str | None = await self._sensors.get_or_create_id_by_type_code(
            locker_id, sensor_type_code, serial_number=f"relay-{sensor_type_code}",
        )
        if sensor_id is not None:
            self._sensor_id_cache[key] = sensor_id
        return sensor_id

    async def _emit_security(self, locker_id: str, sensor_type_code: str | None,
                             violations: list[str]) -> None:
        """Прямой device-asserted security-event (минуя StateMonitor-пороги).

        Device-asserted события (alarm/power-loss/ERROR) эмитятся напрямую
        здесь, а не через StateMonitor: тот sweep не видит их вовсе — он
        матчит только по sensor_thresholds, а не по факту device-flag'а (сам
        sweep при этом отрабатывает и для relay-локеров тоже, порогово).
        Intrusion relay-серии ставит ``_ensure_intrusion``. Никогда не роняет ingest."""
        if self._security_factory is None:
            return
        try:
            sensor_id = None
            if sensor_type_code is not None and self._sensors is not None:
                sensor_id = await self._resolve_sensor_id(locker_id, sensor_type_code)
            producer = self._security_factory(sensor_type_code)
            await producer.publish(locker_id, sensor_id, violations,
                                   sensor_type_code=sensor_type_code)
        except Exception as exc:
            logger.error("Failed to emit security event for locker %s: %s", locker_id, exc)

    async def _emit_offline(self, locker_id: str) -> None:
        """Поставить offline history event в outbound queue.

        Reason heartbeat_timeout — единственный осмысленный для relay-пути:
        offline зафиксирован по отсутствию свежего stat / по stat=0, аналог
        heartbeat-таймаута WS-пути. last_seen_at не прокидываем (нет дешёвого
        кэша, как в WS _cleanup_connection)."""
        if self._offline is None:
            return
        try:
            await self._offline.publish_offline(
                locker_id=locker_id, reason=REASON_HEARTBEAT_TIMEOUT)
        except Exception as exc:
            logger.error("Failed to enqueue OFFLINE event for locker %s: %s",
                         locker_id, exc)

    async def _handle_rep(self, payload: bytes, received_at: datetime) -> None:
        rep = rc.decode_rep(payload)
        locker_id = await self._resolve(rep.device_id)
        if locker_id is None:
            return
        outcome: RelayMessageOutcome = await self._results.handle_rep(
            locker_id, rep.cmd_id, rep.result, received_at=received_at,
        )

        # rep от платы, помеченной offline в БД → переспрашиваем её
        # статус командой SEND_STATUS (command=4) в com/{relay_id}. rep на неё не
        # ожидается; cmd_id=0, data=0; retain=False (one-shot query). Сам факт
        # rep уже доказывает, что плата жива — следующий stat актуализирует
        # connectivity, но явный re-query ускоряет восстановление.
        if await self._is_offline(locker_id):
            try:
                payload_com = encode_com(cmd_id=0, command=Command.SEND_STATUS, data=0)
                await self._mqtt.publish(topic_com(rep.device_id), payload_com,
                                         qos=MqttQoS.AT_LEAST_ONCE, retain=False)
            except Exception as exc:
                logger.error("Failed to publish SEND_STATUS re-query for locker %s: %s",
                             locker_id, exc)
        record_relay_message(outcome)

    async def _is_offline(self, locker_id: str) -> bool:
        """True, если шкаф сейчас помечен connectivity=offline в БД.

        Лёгкая проекция get_connectivity вместо get_by_id (SELECT * + dacite-
        гидрация всей модели): здесь нужно ровно одно поле, и метод зовётся на
        каждый rep."""
        connectivity = await self._repo.get_connectivity(locker_id)
        return bool(connectivity == Connectivity.OFFLINE)

    async def _op_status(self, locker_id: str) -> OperationalStatus | None:
        """OperationalStatus с TTL-кэшем — избегаем SELECT на каждый heartbeat.

        Возвращает OperationalStatus или None (шкаф не найден / archived)."""
        cached = self._op_status_cache.get(locker_id)
        now = time.monotonic()
        if cached is not None and cached[1] > now:
            return cached[0]
        # Локальная аннотация: self._repo — duck-typed Any, но get_operational_status
        # по контракту возвращает OperationalStatus | None; фиксируем тип явно, чтобы
        # не отдавать Any из типизированной функции (no-any-return), без cast.
        status: OperationalStatus | None = await self._repo.get_operational_status(locker_id)
        self._op_status_cache[locker_id] = (status, now + self._op_status_ttl_s)
        return status

    def _telemetry_bucket(self, device_id: int) -> TokenBucket:
        return _lru_bucket(self._telemetry_buckets, device_id,
                           self._telemetry_rate, self._telemetry_burst)

    def _series_bucket(self, device_id: int) -> TokenBucket:
        return _lru_bucket(self._series_buckets, device_id,
                           self._series_rate, self._series_burst)

    async def _handle_photo(self, payload: bytes) -> None:
        photo = rc.decode_photo(payload)
        validate_frame(photo)
        locker_id = await self._resolve(photo.device_id)
        if locker_id is None:
            return
        if self._series_repo is None or self._media is None:
            record_relay_message(RelayMessageOutcome.PROCESSED)
            return
        if photo.photo_id > photo.photos_cnt:
            logger.warning("relay photo photo_id=%s beyond photos_cnt=%s relay=%s event_id=%s",
                           photo.photo_id, photo.photos_cnt, photo.device_id, photo.event_id)
        digest = frame_digest(photo.photo)
        claimed = await self._classify_and_claim(locker_id, photo, digest)
        if claimed is None:
            return
        disposition, series = claimed
        if disposition is FrameDisposition.DUPLICATE:
            logger.info("duplicate relay photo relay=%s event_id=%s photo_id=%s",
                        photo.device_id, photo.event_id, photo.photo_id)
            await self._ensure_intrusion(locker_id, series)
            record_relay_message(RelayMessageOutcome.PROCESSED)
            return

        if series.photos_cnt != photo.photos_cnt:
            logger.warning("relay photo photos_cnt=%s differs from series photos_cnt=%s "
                           "relay=%s event_id=%s", photo.photos_cnt, series.photos_cnt,
                           photo.device_id, photo.event_id)
        if series.total_bytes + len(photo.photo) > self._series_max_bytes:
            logger.warning("dropping relay photo: series relay=%s event_id=%s exceeds %d bytes",
                           photo.device_id, photo.event_id, self._series_max_bytes)
            record_relay_message(RelayMessageOutcome.DROPPED_MALFORMED)
            return
        await self._ensure_intrusion(locker_id, series)
        if not await self._publish_frame(locker_id, series, photo):
            # record_frame не зовём: повтор кадра опубликует его снова с тем же uid.
            record_relay_message(RelayMessageOutcome.HANDLER_ERROR)
            return
        await self._series_repo.record_frame(series.id, photo.photo_id, digest, len(photo.photo))
        record_relay_message(RelayMessageOutcome.PROCESSED)

    async def _classify_and_claim(
        self, locker_id: str, photo: rc.Photo, digest: str,
    ) -> tuple[FrameDisposition, RelayPhotoSeries] | None:
        """Отнести кадр к серии; None — кадр отброшен rate-limit'ом.

        Классификация и захват серии сериализованы на (locker, relay, event_id):
        параллельный кадр той же серии видит уже созданную строку и не тратит
        токен rate-limit. Проигрыш ``create`` остаётся только при гонке между
        процессами — тогда кадр возвращает токен и становится new_frame
        (или duplicate) серии победителя.
        """
        async with self._series_claims.hold((locker_id, photo.device_id, photo.event_id)):
            rows = await self._series_repo.list_for_event(
                locker_id, photo.device_id, photo.event_id, self._series_epoch_s)
            cls = classify_frame(rows, photo.photo_id, digest)
            if cls.series is not None and cls.disposition in (
                    FrameDisposition.DUPLICATE, FrameDisposition.NEW_FRAME):
                record_relay_photo_frame(cls.disposition.value)
                return cls.disposition, cls.series

            bucket = self._series_bucket(photo.device_id)
            if not bucket.try_consume():
                record_relay_photo_frame(cls.disposition.value)
                logger.warning("relay photo series rate-limited relay=%s event_id=%s",
                               photo.device_id, photo.event_id)
                record_relay_message(RelayMessageOutcome.DROPPED_RATE_LIMITED)
                return None
            created = False
            try:
                if cls.series is not None:
                    logger.warning(
                        "relay photo series reset relay=%s event_id=%s "
                        "(event_id reuse or stale epoch)", photo.device_id, photo.event_id)
                    await self._series_repo.supersede(cls.series.id)
                series, created = await self._series_repo.create(
                    locker_id, photo.device_id, photo.event_id, str(uuid.uuid4()),
                    photo.photos_cnt, self._series_epoch_s)
            finally:
                if not created:
                    bucket.refund()
        disposition = cls.disposition
        if not created:
            again = classify_frame([series], photo.photo_id, digest).disposition
            disposition = (FrameDisposition.DUPLICATE if again is FrameDisposition.DUPLICATE
                           else FrameDisposition.NEW_FRAME)
        record_relay_photo_frame(disposition.value)
        return disposition, series

    async def _ensure_intrusion(self, locker_id: str, series: RelayPhotoSeries) -> None:
        """Поставить intrusion серии не больше одного раза. Никогда не роняет ingest."""
        if series.intrusion_enqueued_at is not None or self._security_factory is None:
            return
        try:
            sensor_id = None
            if self._sensors is not None:
                sensor_id = await self._resolve_sensor_id(locker_id, _SENSOR_INTRUSION)
            producer = self._security_factory(_SENSOR_INTRUSION)
            payload = producer.build_payload(
                locker_id, sensor_id, [_INTRUSION_VIOLATION],
                sensor_type_code=_SENSOR_INTRUSION,
                event_uid=series_intrusion_uid(series.series_uid),
                extra_metadata={
                    RelayPhotoMetaKey.RELAY_EVENT_ID.value: series.event_id,
                    RelayPhotoMetaKey.SERIES_UID.value: series.series_uid,
                    RelayPhotoMetaKey.PHOTOS_CNT.value: series.photos_cnt,
                })
            if await self._series_repo.enqueue_intrusion(
                    series.id, producer.CHANNEL, producer.ENDPOINT, payload):
                producer.record_enqueued()
        except Exception as exc:
            logger.error("Failed to enqueue relay photo intrusion for locker %s: %s",
                         locker_id, exc)

    async def _publish_frame(self, locker_id: str, series: RelayPhotoSeries,
                             photo: rc.Photo) -> bool:
        """Поставить кадр в outbox. Никогда не бросает; False — кадр не поставлен."""
        try:
            meta = MediaTransferMeta(
                transfer_id=(f"relay-{series.relay_id}-{series.event_id}-"
                             f"{photo.photo_id}-{series.series_uid[:8]}"),
                kind=_PHOTO_KIND,
                mime_type=_PHOTO_MIME,
                size_bytes=len(photo.photo),
                captured_at=series.first_seen_at.isoformat(),
                sensor_id=None,
                width=None,
                height=None,
                duration_ms=None,
                security_event_uid=series_intrusion_uid(series.series_uid),
                metadata=frame_metadata(series, photo),
            )
            await self._media.publish(
                result=MediaTransferResult(meta=meta, bytes_=photo.photo),
                locker_id=locker_id,
                security_event_id=None,
                event_uid=frame_media_uid(series.series_uid, photo.photo_id),
            )
        except Exception as exc:
            logger.error("Failed to publish relay photo frame for locker %s: %s",
                         locker_id, exc)
            return False
        return True

    async def run_photo_maintenance(self, *, retention_days: int) -> None:
        """Дозаэмитить intrusion зависших серий и вычистить старые. Никогда не бросает."""
        if self._series_repo is None:
            return
        try:
            pending = await self._series_repo.list_pending_intrusion(
                _PENDING_INTRUSION_AGE_S, _PENDING_INTRUSION_BATCH, self._series_epoch_s)
            for series in pending:
                await self._ensure_intrusion(series.locker_id, series)
        except Exception:
            logger.exception("relay photo pending-intrusion sweep failed")
        try:
            deleted = await self._series_repo.purge_older_than(retention_days)
            if deleted:
                logger.info("purged %d relay photo series older than %d days",
                            deleted, retention_days)
        except Exception:
            logger.exception("relay photo series purge failed")


@dataclass
class _KeyedLock:
    lock: asyncio.Lock
    holders: int = 0


class _KeyedLocks:
    """asyncio.Lock на ключ; запись живёт, пока лок держат или ждут."""

    def __init__(self) -> None:
        self._locks: dict[tuple[str, int, int], _KeyedLock] = {}

    @asynccontextmanager
    async def hold(self, key: tuple[str, int, int]) -> AsyncIterator[None]:
        entry = self._locks.get(key)
        if entry is None:
            entry = self._locks[key] = _KeyedLock(asyncio.Lock())
        entry.holders += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.holders -= 1
            if entry.holders == 0:
                del self._locks[key]

    def __len__(self) -> int:
        return len(self._locks)


def _lru_bucket(store: OrderedDict[int, TokenBucket], key: int,
                rate: float, burst: float) -> TokenBucket:
    """TokenBucket для key с LRU-вытеснением самого давнего при переполнении."""
    bucket = store.get(key)
    if bucket is None:
        bucket = TokenBucket(rate=rate, capacity=burst)
        store[key] = bucket
        if len(store) > _BUCKETS_MAX:
            store.popitem(last=False)
    else:
        store.move_to_end(key)
    return bucket
