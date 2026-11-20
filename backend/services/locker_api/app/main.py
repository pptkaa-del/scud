import asyncio
from logging import getLogger
import os
import signal

import psycopg2
import tornado.ioloop
import tornado.web

from shared.auth import (
    HealthHandler,
    LivenessHandler,
    ReadinessHandler,
    db_check,
    mqtt_check,
)
from shared.database import AsyncPostgres, PostgresConfig
from shared.http import RetryHTTPClient
from shared.metrics import (
    register_metric_sampler,
    register_pool,
    set_command_pending,
    set_mqtt_relays_online,
)
from shared.metrics.handler import MetricsHandler
from shared.mqtt_client import MqttClient, MqttQoS
from shared.observability import configure_logging
from shared.utils import is_production, read_secret

from .handlers._step_up import Require2FAForCommandsMixin
from .handlers.command import CommandDetailHandler, CommandListHandler
from .handlers.command_confirm import CommandConfirmHandler
from .handlers.locker import (
    LockerBatchHandler,
    LockerDetailHandler,
    LockerListHandler,
    LockerStateHandler,
)
from .handlers.relay import RelayDetailHandler, RelayListHandler
from .handlers.sensor import SensorDetailHandler, SensorListHandler
from .handlers.sensor_type import SensorTypeDetailHandler, SensorTypeListHandler
from .handlers.threshold import ThresholdDetailHandler, ThresholdListHandler
from .locker_data.db.repositories.command import CommandRepository
from .mqtt import relay_codec as rc
from .mqtt.bulkhead import IngestBulkhead
from .mqtt.dispatch import make_photo_bulkhead, make_relay_dispatch
from .mqtt.ingest import RelayIngest
from .services.auth_step_up_client import AuthStepUpClient
from .services.command_expiry import DEFAULT_COMMAND_TTL_S, CommandExpiry
from .services.command_queue import CommandQueue
from .services.history_producers.step_up_event_producer import StepUpEventProducer
from .services.notification_client import NotificationClient
from .services.outbound_dispatcher import OutboundDispatcher
from .services.state_monitor import StateMonitor

logger = getLogger(__name__)

STEP_UP_ISSUE_TIMEOUT_ENV = "STEP_UP_ISSUE_TIMEOUT_SECONDS"
DEFAULT_STEP_UP_ISSUE_TIMEOUT_SECONDS = 50.0


def build_step_up_http() -> RetryHTTPClient:
    # auth_api на issue синхронно ждёт доставки OTP (до ~45 с): таймаут обязан
    # быть больше. POST без idempotent=True на таймауте не ретраится (дубль кода).
    timeout = float(
        os.getenv(STEP_UP_ISSUE_TIMEOUT_ENV, str(DEFAULT_STEP_UP_ISSUE_TIMEOUT_SECONDS))
    )
    return RetryHTTPClient(request_timeout=timeout)


def _read_history_hmac_key() -> bytes | None:
    """Прочитать общий с history_api HMAC-ключ (SEC-05).

    Подписывает исходящие POST на /internal/api/v1/{events/security,telemetry,
    media} (OutboundDispatcher + StepUpEventProducer). ОБЯЗАН совпадать со
    значением в history_api. None → запросы летят без подписи (dev/CI).
    """
    key_str = read_secret(
        "internal_history_hmac_key",
        env_var="INTERNAL_HISTORY_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    if not key_str:
        logger.warning(
            "[SECURITY] INTERNAL_HISTORY_HMAC_KEY is not set — outgoing "
            "/internal/api/v1/{events/security,telemetry,media} requests to "
            "history_api will be UNSIGNED. Set the same value in history_api, "
            "locker_api and auth_api before production deploy.",
        )
        return None
    return key_str.encode("utf-8")


def make_app(
    db: AsyncPostgres,
    command_queue: CommandQueue,
    notification_client: NotificationClient | None = None,
    mqtt_client: MqttClient | None = None,
) -> tornado.web.Application:
    """Build the Tornado app.

    `notification_client` опционален: production-сборка передаёт
    singleton-инстанс; интеграционные тесты, которые мокают БД, могут
    оставить его None — тогда WS-handler создаст разовый клиент только
    при первом обращении.

    ``auth_step_up_client`` + ``step_up_mixin`` всегда создаются — это
    in-process singletons; сетевой вызов происходит только когда у
    пользователя ``X-Remote-Require-2FA: true`` и команда привилегированная,
    поэтому non-2FA flow (включая существующие unit-тесты) не делает
    HTTP-запросов к auth_api.
    """
    debug = os.getenv("DEBUG", "false").lower() == "true"

    auth_api_url = os.getenv("AUTH_API_URL", "http://auth_api:8003")
    step_up_hmac_key_str = read_secret(
        "internal_step_up_hmac_key",
        env_var="INTERNAL_STEP_UP_HMAC_KEY",
        default="",
        required=is_production(),
    ) or ""
    step_up_hmac_key: bytes | None = (
        step_up_hmac_key_str.encode("utf-8") if step_up_hmac_key_str else None
    )
    if step_up_hmac_key is None:
        logger.warning(
            "[SECURITY] INTERNAL_STEP_UP_HMAC_KEY is not set — outgoing "
            "/internal/api/v1/step-up/* requests to auth_api will be UNSIGNED. "
            "Set the same value in auth_api and locker_api before production deploy."
        )
    auth_step_up_client = AuthStepUpClient(
        auth_api_url, http=build_step_up_http(), hmac_key=step_up_hmac_key,
    )
    step_up_mixin = Require2FAForCommandsMixin(auth_step_up_client)

    history_api_url = os.getenv("HISTORY_API_URL", "http://history_api:8002")
    history_hmac_key = _read_history_hmac_key()
    security_event_producer = StepUpEventProducer(history_api_url, hmac_key=history_hmac_key)

    return tornado.web.Application([
        # Health-depth: /live (рестарт-проба, без БД), /ready (db critical +
        # mqtt soft — gmqtt-сессия к брокеру), /health (DB-only back-compat).
        (r"/live", LivenessHandler),
        (r"/ready", ReadinessHandler, dict(
            checks=[db_check(db)] + (
                [mqtt_check(mqtt_client)] if mqtt_client is not None else []
            ),
        )),
        (r"/health", HealthHandler, dict(db=db)),
        # Prometheus scrape endpoint. См. user_api/main.py.
        (r"/metrics", MetricsHandler),

        # Control Gateway - Управление шкафами (коллекция — plural)
        (r"/api/v1/lockers", LockerListHandler, dict(db=db)),
        (r"/api/v1/lockers/batch", LockerBatchHandler, dict(db=db)),

        # Control Gateway - Управление шкафами (экземпляр — singular)
        (r"/api/v1/locker/([^/]+)/state", LockerStateHandler, dict(db=db)),
        (r"/api/v1/locker/([^/]+)/relays", RelayListHandler, dict(db=db)),
        (r"/api/v1/locker/([^/]+)/relays/([1-4])", RelayDetailHandler, dict(db=db)),
        (r"/api/v1/locker/([^/]+)", LockerDetailHandler, dict(db=db)),

        # Control Gateway - Управление типами датчиков
        (r"/api/v1/sensor-types", SensorTypeListHandler, dict(db=db)),
        (r"/api/v1/sensor-types/([^/]+)", SensorTypeDetailHandler, dict(db=db)),

        # Control Gateway - Управление датчиками (singular parent)
        (r"/api/v1/locker/([^/]+)/sensors", SensorListHandler, dict(db=db)),
        (r"/api/v1/locker/([^/]+)/sensors/([^/]+)", SensorDetailHandler, dict(db=db)),

        # Control Gateway - Управление пороговыми значениями
        (r"/api/v1/locker/([^/]+)/sensors/([^/]+)/thresholds", ThresholdListHandler, dict(db=db)),
        (r"/api/v1/locker/([^/]+)/sensors/([^/]+)/thresholds/([^/]+)", ThresholdDetailHandler, dict(db=db)),

        # Control Gateway - Управление командами
        (r"/api/v1/locker/([^/]+)/commands", CommandListHandler, dict(db=db, command_queue=command_queue)),
        # Step-up 2FA подтверждение (должен идти ДО /commands/{id}, более специфичный)
        (
            r"/api/v1/locker/([0-9a-f-]{36})/commands/confirm",
            CommandConfirmHandler,
            dict(db=db, command_queue=command_queue),
        ),
        (r"/api/v1/locker/([^/]+)/commands/([^/]+)", CommandDetailHandler, dict(db=db)),
    ],
        debug=debug,
        notification_client=notification_client,
        mqtt_client=mqtt_client,
        auth_step_up_client=auth_step_up_client,
        step_up_mixin=step_up_mixin,
        security_event_producer=security_event_producer,
    )


async def reset_stale_online_lockers(db: AsyncPostgres) -> int:
    """На старте сбрасывает connectivity=ONLINE → OFFLINE.

    После нечистого шатдауна (SIGKILL, OOM) `on_close → _cleanup_connection`
    мог не отработать, и в БД остались шкафы с connectivity=online без
    действующего WS-соединения. Реальные онлайны переподключатся в течение
    минуты, обновят запись через `_handle_auth_response → update_connectivity(ONLINE)`.
    """
    sql = """
    WITH updated AS (
        UPDATE lockers
        SET connectivity = 'offline'
        WHERE connectivity = 'online'
        RETURNING id
    )
    SELECT COUNT(*) AS count FROM updated
    """
    row = await db.fetch_one(sql, (), read_only=False)
    count = int(row["count"]) if row and row.get("count") is not None else 0
    if count > 0:
        logger.info("Reset %d stale ONLINE lockers to OFFLINE on startup", count)
    return count


async def reset_stale_sent_commands(db: AsyncPostgres) -> int:
    """Сбрасывает команды, застрявшие в статусе 'sent' >5 минут, обратно в 'pending'.

    Если сервис упал между write_message и device_handler.on_message, ACK от
    устройства уже не придёт, и команда висит в 'sent' навсегда. Hook
    возвращает её в очередь для повторной доставки.

    Выполняется после TTL-drain и имеет смысл только при COMMAND_TTL_S > 5 мин.

    Только команды с ОСТАВШИМСЯ retry-бюджетом (retry_count < max_retries)
    воскрешаются в pending (COR-15): исчерпавшие бюджет НЕ резурректятся (иначе
    get_pending_commands их всё равно не подберёт — фильтр retry_count <
    max_retries — и они зависнут зомби в pending). Их подберёт первый же тик
    ack-timeout sweep и переведёт в терминальный failed.
    """
    sql = """
    WITH updated AS (
        UPDATE locker_commands
        SET status = 'pending'
        WHERE status = 'sent'
          AND sent_at < NOW() - INTERVAL '5 minutes'
          AND retry_count < max_retries
        RETURNING id
    )
    SELECT COUNT(*) AS count FROM updated
    """
    row = await db.fetch_one(sql, (), read_only=False)
    count = int(row["count"]) if row and row.get("count") is not None else 0
    if count > 0:
        logger.warning("Reset %d stale sent commands to pending on startup", count)
    return count


def build_command_expiry(ack_timeout_minutes: int) -> CommandExpiry:
    """CommandExpiry из COMMAND_TTL_S; неположительный TTL валит старт (fail-closed)."""
    command_ttl_s = int(os.getenv("COMMAND_TTL_S", str(DEFAULT_COMMAND_TTL_S)))
    expiry = CommandExpiry(command_ttl_s)
    if command_ttl_s > ack_timeout_minutes * 60:
        logger.warning(
            "COMMAND_TTL_S=%s exceeds ack timeout %s min: ack-timeout resend/retry "
            "can run before TTL expiry",
            command_ttl_s, ack_timeout_minutes,
        )
    return expiry


async def run_command_boot_hooks(db: AsyncPostgres, expiry: CommandExpiry) -> None:
    """TTL-drain просроченных команд, затем возврат зависших 'sent' в очередь."""
    try:
        expired = await expiry.drain(CommandRepository(db))
        if expired:
            logger.info("Expired %d stale commands on startup", expired)
    except Exception:
        logger.exception("Command TTL sweep on startup failed")
    try:
        await reset_stale_sent_commands(db)
    except Exception:
        logger.exception("Failed to reset stale sent commands on startup")


async def wait_for_db(config: PostgresConfig) -> AsyncPostgres:
    """Ожидание готовности БД с ретраями и экспоненциальной задержкой"""
    attempts, delay, max_attempts = 0, 1.0, 30
    while True:
        try:
            return AsyncPostgres(config, statement_timeout_ms=5000)
        except psycopg2.OperationalError as e:
            attempts += 1
            if attempts >= max_attempts:
                logger.error(f"Failed to connect to DB after {max_attempts} attempts.")
                raise
            logger.warning(f"DB not ready ({e}); retrying in {delay:.1f}s [{attempts}/{max_attempts}]...")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 10.0)


RELAYS_ONLINE_REFRESH_S = float(os.getenv("MQTT_RELAYS_ONLINE_REFRESH_S", "30"))
INGEST_MAX_CONCURRENCY = int(os.getenv("MQTT_INGEST_MAX_CONCURRENCY", "10"))
INGEST_MAX_PENDING = int(os.getenv("MQTT_INGEST_MAX_PENDING", "1000"))
INGEST_PHOTO_MAX_CONCURRENCY = int(os.getenv("MQTT_INGEST_PHOTO_MAX_CONCURRENCY", "2"))
INGEST_PHOTO_MAX_PENDING_BYTES = int(
    os.getenv("MQTT_INGEST_PHOTO_MAX_PENDING_BYTES", str(256 * 1024 * 1024)))
RELAY_PHOTO_MAINTENANCE_INTERVAL_S = float(os.getenv("RELAY_PHOTO_MAINTENANCE_INTERVAL_S", "60"))
RELAY_PHOTO_SERIES_RETENTION_DAYS = int(os.getenv("RELAY_PHOTO_SERIES_RETENTION_DAYS", "90"))
RELAY_PHOTO_SERIES_EPOCH_S = float(os.getenv("RELAY_PHOTO_SERIES_EPOCH_S", "86400"))
RELAY_PHOTO_SERIES_MAX_BYTES = int(
    os.getenv("RELAY_PHOTO_SERIES_MAX_BYTES", str(rc.MAX_RELAY_PHOTO_BYTES)))
RELAY_PHOTO_SERIES_RATE = float(os.getenv("RELAY_PHOTO_SERIES_RATE", "0.1"))
RELAY_PHOTO_SERIES_BURST = float(os.getenv("RELAY_PHOTO_SERIES_BURST", "10"))

_SQL_RELAYS_ONLINE = (
    "SELECT COUNT(*) AS n FROM lockers "
    "WHERE transport = 'mqtt' AND connectivity = 'online'"
)


async def refresh_relays_online_gauge(db: AsyncPostgres, interval_s: float) -> None:
    """Периодически обновлять scud_mqtt_relays_online одним cheap COUNT.

    Вынесено из scrape-пути (он синхронный) и из OfflineMonitor._sweep_once
    (его не трогаем). set_mqtt_relays_online — never-raises; здесь ловим только
    ошибку самого запроса, чтобы таск не умер и продолжил тикать.
    """
    while True:
        try:
            row = await db.fetch_one(_SQL_RELAYS_ONLINE, (), read_only=True)
            count = int(row["n"]) if row and row.get("n") is not None else 0
            set_mqtt_relays_online(count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("relays-online gauge refresh failed", exc_info=True)
        await asyncio.sleep(interval_s)


async def relay_photo_maintenance_loop(
    ingest: RelayIngest, interval_s: float, retention_days: int,
) -> None:
    """Периодически дозаэмитить intrusion зависших серий фото и чистить старые серии."""
    while True:
        await ingest.run_photo_maintenance(retention_days=retention_days)
        await asyncio.sleep(interval_s)


PENDING_COMMANDS_REFRESH_S = float(os.getenv("PENDING_COMMANDS_REFRESH_S", "30"))


async def refresh_pending_commands_gauge(db: AsyncPostgres, interval_s: float) -> None:
    """Периодически обновлять scud_command_pending_total одним cheap COUNT.

    Вынесено из scrape-пути (он синхронный) — COUNT требует await. Зеркалит
    refresh_relays_online_gauge: never-raises helper + CancelledError-clean.
    """
    repo = CommandRepository(db)
    while True:
        try:
            count = await repo.count_pending()
            set_command_pending(count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("pending-commands gauge refresh failed", exc_info=True)
        await asyncio.sleep(interval_s)


async def main() -> None:
    configure_logging(os.getenv("SERVICE_NAME", "locker_api"))
    # пароль БД читается из Docker secret или env-fallback.
    db_password = read_secret(
        "postgres_locker_password", env_var="DB_PASSWORD", default="postgres"
    )
    # Настройки БД из окружения или дефолтные
    config = PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "postgres"),
        password=db_password,
        database=os.getenv("DB_NAME", "scud"),
    )

    # Ожидание готовности БД
    db = await wait_for_db(config)

    # Pull-based DB-pool-saturation метрики: provider семплируется на каждый
    # scrape /metrics, не на hot-path запросов.
    register_pool(db.pool_name, db.get_pool_info)

    ack_timeout_minutes = int(os.getenv("COMMAND_ACK_TIMEOUT_MINUTES", "5"))
    command_expiry = build_command_expiry(ack_timeout_minutes)
    await run_command_boot_hooks(db, command_expiry)

    # Сбрасываем connectivity у шкафов, оставшихся ONLINE после нечистого шатдауна.
    try:
        await reset_stale_online_lockers(db)
    except Exception:
        logger.exception("Failed to reset stale ONLINE lockers on startup")

    # In-process реестр транспортов устройств (CommandQueue._connections)
    # привязан к этой реплике: доставка WS/MQTT-команд работает только при
    # единственной реплике locker_api (см. DEPLOYMENT.md).
    logger.info(
        "locker_api uses an in-process device transport registry; "
        "run exactly one replica or WS/MQTT command delivery breaks silently",
    )

    command_queue = CommandQueue(expiry=command_expiry)

    # CommandHistoryProducer для ack-timeout sweep (failed-событие, COR-15) и
    # 'sent'-событий queue-processor'а. Создаём всегда, а не только в mqtt-ветке.
    from .services.history_producers.command_history_producer import (
        CommandHistoryProducer as _CommandHistoryProducer,
    )
    command_history_producer_common = _CommandHistoryProducer(db)

    # --- MQTT релейный транспорт (Plan 2) ---------------------------------
    # Включается, когда задан MQTT_HOST (compose выставляет его для locker_api).
    # Подписываемся на shared stat/rep/photos, демультиплексируем по relay_id,
    # обновляем connectivity/телеметрию через те же producer'ы, что и WS-путь,
    # и публикуем бинарные команды в com/{relay_id} (через CommandQueue).
    # Singleton NotificationClient: создаётся один раз при старте сервиса и
    # шарится через application.settings, чтобы избежать лишних
    # RetryHTTPClient на каждое WS-сообщение. Создаётся ДО MQTT-блока, т.к.
    # RelayCommandResults получает его для паритета по уведомлениям (SIMP-10).
    notification_client = NotificationClient()

    mqtt_enabled = os.getenv("MQTT_HOST") is not None
    mqtt_client = None
    offline_monitor = None
    relay_ingest: RelayIngest | None = None
    if mqtt_enabled:
        from .locker_data.db.repositories.command import CommandRepository
        from .locker_data.db.repositories.locker import LockerRepository
        from .locker_data.db.repositories.relay import RelayRepository
        from .locker_data.db.repositories.relay_photo_series import (
            RelayPhotoSeriesRepository,
        )
        from .locker_data.db.repositories.sensor import SensorRepository
        from .mqtt.offline_monitor import OfflineMonitor
        from .mqtt.results import RelayCommandResults
        from .mqtt.sys_metrics import SysMetricsHandler, sample_mqtt_connected
        from .services.history_producers.command_history_producer import (
            CommandHistoryProducer,
        )
        from .services.history_producers.media_producer import MediaProducer
        from .services.history_producers.offline_producer import OfflineEventProducer
        from .services.history_producers.security_event_producer import (
            CommandLateExecutionProducer,
            producer_for_sensor_type,
        )
        from .services.history_producers.telemetry_producer import TelemetryProducer

        # Producer'ы ставят payload в outbound queue (db), а не шлют HTTP сами —
        # конструктор принимает AsyncPostgres (НЕ history_api_url; см. _base.py).
        # history_api_url доставляется в payload через OutboundDispatcher.
        telemetry_producer = TelemetryProducer(db)
        command_history_producer = CommandHistoryProducer(db)
        offline_producer = OfflineEventProducer(db)
        mqtt_client = MqttClient(
            host=os.environ["MQTT_HOST"],
            port=int(os.getenv("MQTT_PORT", "1883")),
            client_id="scud-locker-api",
            username=os.getenv("MQTT_USER"),
            # Docker secret mqtt_password → env MQTT_PASSWORD → None. Брокер пишет
            # тот же пароль в passwordfile из того же секрета/env (см. mqtt/entrypoint.sh).
            password=read_secret("mqtt_password", env_var="MQTT_PASSWORD"),
            keepalive=60,
        )
        relay_results = RelayCommandResults(
            command_repo=CommandRepository(db),
            history_producer=command_history_producer,
            # Паритет с WS-путём (SIMP-10): OPEN/CLOSE rep синхронизирует
            # door-state и persistent-уведомление через общий CommandResultApplier.
            locker_repo=LockerRepository(db),
            notification_client=notification_client,
            command_ttl_s=command_expiry.ttl_s,
            late_execution_producer=CommandLateExecutionProducer(db),
        )
        relay_ingest = RelayIngest(
            repo=LockerRepository(db),
            command_queue=command_queue,
            command_results=relay_results,
            mqtt_client=mqtt_client,
            telemetry=telemetry_producer,
            offline_producer=offline_producer,
            sensor_repo=SensorRepository(db),
            # MqttTransport, построенный ingest'ом, сам выделит relay_cmd_id в
            # prepare_send через этот аллокатор-порт — CommandQueue об mqtt не знает.
            allocator=CommandRepository(db),
            # device-asserted security events (alarm, power-loss, ERROR): маршрутизация
            # по sensor_type_code, идентична StateMonitor — tamper → DoorTamperProducer,
            # остальные → ThresholdViolationProducer.
            security_producer_factory=lambda code: producer_for_sensor_type(db, code),
            # Кадры photos → history_api locker_media (по кадру, outbox, dedup по uuid5
            # серии); серии и их intrusion-события ведёт реестр relay_photo_series.
            media_producer=MediaProducer(db),
            photo_series_repo=RelayPhotoSeriesRepository(db),
            photo_series_epoch_s=RELAY_PHOTO_SERIES_EPOCH_S,
            photo_series_max_bytes=RELAY_PHOTO_SERIES_MAX_BYTES,
            photo_series_rate=RELAY_PHOTO_SERIES_RATE,
            photo_series_burst=RELAY_PHOTO_SERIES_BURST,
            telemetry_rate=float(os.getenv("RELAY_TELEMETRY_RATE", "1.0")),
            telemetry_burst=float(os.getenv("RELAY_TELEMETRY_BURST", "5.0")),
            command_repo=CommandRepository(db),
            notification_client=notification_client,
            relay_repo=RelayRepository(db),
        )
        sys_metrics = SysMetricsHandler()

        from .mqtt.relay_protocol import device_subscriptions

        # $SYS → broker-stat gauges; stat/rep и photos — в раздельные полосы
        # bulkhead, чтобы шторм или поток крупных кадров не исчерпал пул БД.
        mqtt_client.set_message_handler(make_relay_dispatch(
            relay_ingest, sys_metrics,
            IngestBulkhead(INGEST_MAX_CONCURRENCY, INGEST_MAX_PENDING),
            make_photo_bulkhead(INGEST_PHOTO_MAX_CONCURRENCY, INGEST_PHOTO_MAX_PENDING_BYTES),
        ))
        # Обе схемы публикации: общий топик и per-id (ПИВ v2). Кадр из
        # `<kind>/{id}` проверяется на совпадение id с телом в ingest.
        for _sub in device_subscriptions():
            mqtt_client.add_subscription(_sub, MqttQoS.AT_LEAST_ONCE)
        # $SYS broker stats для scud_mqtt_broker_* (QoS0 — брокер их шлёт
        # периодически, потеря одного фрейма некритична). Реплеится на каждый
        # reconnect через MqttClient._on_connect.
        mqtt_client.add_subscription("$SYS/broker/#", MqttQoS.AT_MOST_ONCE)
        # scrape-time sampler: scud_mqtt_connected отражает живую сессию.
        register_metric_sampler(lambda: sample_mqtt_connected(mqtt_client))
        offline_monitor = OfflineMonitor(db, command_queue, offline_producer=offline_producer)

    # Инициализация State Monitor
    state_monitor = StateMonitor(db, notification_client=notification_client)

    # Env-tunable периодические интервалы (production defaults; override через .env.fast для E2E)
    state_monitor_interval = int(os.getenv("STATE_MONITOR_CHECK_INTERVAL_S", "60"))
    queue_processor_interval = int(os.getenv("COMMAND_QUEUE_PROCESSOR_INTERVAL_S", "5"))
    ack_timeout_interval = int(os.getenv("COMMAND_ACK_TIMEOUT_LOOP_INTERVAL_S", "60"))
    dispatcher_poll = float(os.getenv("OUTBOUND_DISPATCHER_POLL_INTERVAL_S", "2.0"))
    # Бюджет батча по суммарному размеру payload'ов (default 32 МиБ): не даёт
    # батчу из крупных media-фото загрузить сотни МБ разом (тот самый OOM,
    # задушивший dispatcher). Конкурентность доставки и порог dead-letter — рядом.
    dispatcher_max_batch_bytes = int(
        os.getenv("OUTBOUND_DISPATCHER_MAX_BATCH_BYTES", str(32 * 1024 * 1024))
    )
    dispatcher_concurrency = int(os.getenv("OUTBOUND_DISPATCHER_CONCURRENCY", "8"))
    dispatcher_max_attempts = int(os.getenv("OUTBOUND_DISPATCHER_MAX_ATTEMPTS", "20"))

    # Инициализация Outbound Dispatcher (доставка событий в history_api)
    outbound_dispatcher = OutboundDispatcher(
        db,
        poll_interval_seconds=dispatcher_poll,
        retention_days=int(os.getenv("OUTBOUND_RETENTION_DAYS", "7")),
        hmac_key=_read_history_hmac_key(),
        max_batch_bytes=dispatcher_max_batch_bytes,
        concurrency=dispatcher_concurrency,
        max_attempts=dispatcher_max_attempts,
    )

    # Создание приложения
    app = make_app(db, command_queue, notification_client, mqtt_client=mqtt_client)
    port = int(os.getenv("APP_PORT", 8001))
    # xheaders=True — request.remote_ip читает X-Real-IP / X-Forwarded-For
    # (gateway проставляет X-Real-IP). Важно для logging WS-подключений и
    # любого будущего per-IP rate-limit.
    app.listen(port, xheaders=True)
    logger.info(f"Locker API started on port {port}")

    # Запуск фоновых сервисов
    command_queue_task = asyncio.create_task(
        command_queue.start_queue_processor(
            db,
            check_interval=queue_processor_interval,
            history_producer=command_history_producer_common,
        )
    )
    ack_timeout_task = asyncio.create_task(
        command_queue.start_ack_timeout_loop(
            db,
            interval_s=ack_timeout_interval,
            timeout_min=ack_timeout_minutes,
            history_producer=command_history_producer_common,
        )
    )
    state_monitor_task = asyncio.create_task(
        state_monitor.start_monitoring(check_interval=state_monitor_interval)
    )
    outbound_dispatcher_task = asyncio.create_task(outbound_dispatcher.start())
    pending_commands_task = asyncio.create_task(
        refresh_pending_commands_gauge(db, PENDING_COMMANDS_REFRESH_S)
    )

    # MQTT-клиент (gmqtt с авто-reconnect) + offline-monitor запускаются только
    # когда сконфигурирован брокер (MQTT_HOST).
    mqtt_task: asyncio.Task[None] | None = None
    offline_task: asyncio.Task[None] | None = None
    relays_online_task: asyncio.Task[None] | None = None
    photo_maintenance_task: asyncio.Task[None] | None = None
    if mqtt_enabled and mqtt_client is not None and offline_monitor is not None:
        mqtt_task = asyncio.create_task(mqtt_client.run_forever())
        offline_task = asyncio.create_task(offline_monitor.start())
        relays_online_task = asyncio.create_task(
            refresh_relays_online_gauge(db, RELAYS_ONLINE_REFRESH_S)
        )
        if relay_ingest is not None:
            photo_maintenance_task = asyncio.create_task(relay_photo_maintenance_loop(
                relay_ingest, RELAY_PHOTO_MAINTENANCE_INTERVAL_S,
                RELAY_PHOTO_SERIES_RETENTION_DAYS,
            ))

    logger.info(
        "Background services started: Command Queue (%ds), ACK Timeout (%ds), "
        "State Monitor (%ds), Outbound Dispatcher (poll %.1fs)%s",
        queue_processor_interval, ack_timeout_interval, state_monitor_interval, dispatcher_poll,
        ", MQTT relay transport" if mqtt_enabled else "",
    )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    logger.info("Locker API started, waiting for shutdown signal")
    try:
        await stop_event.wait()
    finally:
        logger.info("Shutting down...")
        outbound_dispatcher.stop()
        state_monitor.stop_monitoring()
        command_queue.stop_queue_processor()
        command_queue.stop_ack_timeout_loop()
        if offline_monitor is not None:
            offline_monitor.stop()
        # MQTT/offline/periodic-gauge таски не имеют флага graceful-stop —
        # отменяем и дожидаемся завершения вместе с остальными.
        for task in (mqtt_task, offline_task, relays_online_task, pending_commands_task,
                     photo_maintenance_task):
            if task is not None:
                task.cancel()
        await outbound_dispatcher_task
        await state_monitor_task
        await command_queue_task
        await ack_timeout_task
        for task in (mqtt_task, offline_task, relays_online_task, pending_commands_task,
                     photo_maintenance_task):
            if task is not None:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
