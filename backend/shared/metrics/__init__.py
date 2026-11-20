"""Prometheus metrics for ScudDocs backend services .

Все backend-сервисы экспортируют единый набор метрик на ``/metrics`` в
Prometheus exposition format. Эндпоинт без аутентификации, доступен только
из ``backend_net`` — Apache gateway его наружу не проксирует.

Собираемые метрики:

* ``scud_http_requests_total`` (Counter) — суммарный счётчик HTTP-запросов
  по `{service, method, route, status}`.
* ``scud_http_request_duration_seconds`` (Histogram) — длительность
  HTTP-запросов по `{service, method, route}`.
* ``scud_db_query_duration_seconds`` (Histogram) — длительность DB-запросов
  по `{service, operation}` (operation ∈ {execute, fetch_one, fetch_all}).

Имя сервиса (label ``service``) берётся из переменной окружения
``SERVICE_NAME``, выставляемой ``docker-compose.yaml`` для каждого
backend-контейнера. При отсутствии — ``unknown`` (signal про забытый env).

Cardinality bounded: route нормализуется (`_normalize_route`) до regex-формы
(``/api/v1/locker/:id/sensors`` вместо ``/api/v1/locker/<uuid>/sensors``),
чтобы число серий оставалось конечным даже под 1000 шкафов.
"""
from __future__ import annotations

from collections.abc import Callable
from enum import StrEnum
import logging
import os
import re
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from ..enums import InterserviceStatusClass, RelayMessageOutcome, WsLifecycleEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Service-name resolution (label value)
# ---------------------------------------------------------------------------

SERVICE_NAME: str = os.getenv("SERVICE_NAME", "unknown")
"""Имя сервиса для label `service` во всех метриках.

Выставляется через docker-compose.yaml на каждый backend-контейнер.
"""

# ---------------------------------------------------------------------------
# Metric definitions
# ---------------------------------------------------------------------------

http_requests_total = Counter(
    "scud_http_requests_total",
    "Total HTTP requests by service/method/route/status",
    ["service", "method", "route", "status"],
)

http_request_duration_seconds = Histogram(
    "scud_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)

db_query_duration_seconds = Histogram(
    "scud_db_query_duration_seconds",
    "DB query latency in seconds",
    ["service", "operation"],
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5),
)

# DB connection-pool saturation. Pull-based: значения семплируются
# update_pool_gauges() прямо в MetricsHandler.get() на каждый scrape, а не из
# hot-path acquire/release (нулевой оверхед на запросах). Label `pool` — имя БД.
db_pool_connections_in_use = Gauge(
    "scud_db_pool_connections_in_use",
    "DB connections currently checked out of the pool",
    ["service", "pool"],
)

db_pool_connections_idle = Gauge(
    "scud_db_pool_connections_idle",
    "Idle DB connections available in the pool",
    ["service", "pool"],
)

db_pool_connections_waiters = Gauge(
    "scud_db_pool_connections_waiters",
    "Coroutines currently waiting for a free DB connection",
    ["service", "pool"],
)

db_pool_connections_max = Gauge(
    "scud_db_pool_connections_max",
    "Configured maximum size of the DB connection pool",
    ["service", "pool"],
)

# Step-up 2FA для команд шкафа: cardinality ограничена 4 (outcome) и 2 (status)
# значениями label, метки можно агрегировать в Prometheus без блоу-апа.
command_2fa_challenges_total = Counter(
    "scud_command_2fa_challenges_total",
    "Total step-up 2FA challenge lifecycle events by outcome.",
    ["outcome"],
)

command_2fa_delivery_total = Counter(
    "scud_command_2fa_delivery_total",
    "Total step-up 2FA OTP delivery attempts by status.",
    ["status"],
)

# Межсервисные HTTP-вызовы (shared.http.RetryHTTPClient). target — хост из URL
# (Docker-имя сервиса, 7 значений), method — HTTP-метод, outcome — см.
# InterserviceOutcome. Cardinality ≈ 7×8×4 — ограничена.
interservice_requests_total = Counter(
    "scud_interservice_requests_total",
    "Inter-service HTTP call outcomes by target/method/outcome.",
    ["service", "target", "method", "outcome"],
)

# Латентность межсервисных вызовов (whole fetch incl. retries). status_class ∈
# {2xx, 4xx, 5xx, network_error} — 4 значения; cardinality ≈ 7×8×4 = 224 серий.
# Observe-точки: каждый terminal exit RetryHTTPClient.fetch() (ровно 1 раз на вызов).
interservice_request_duration_seconds = Histogram(
    "scud_interservice_request_duration_seconds",
    "Inter-service HTTP call latency (whole fetch incl. retries) in seconds.",
    ["service", "target", "method", "status_class"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0),
)


def record_interservice_duration(
    target: str, method: str, status_class: InterserviceStatusClass, seconds: float
) -> None:
    """Observe latency on scud_interservice_request_duration_seconds. Никогда не кидает."""
    try:
        interservice_request_duration_seconds.labels(
            service=SERVICE_NAME,
            target=target,
            method=method.upper(),
            status_class=status_class.value,
        ).observe(seconds)
    except Exception:
        logger.debug("Failed to record interservice duration", exc_info=True)

# Ошибки DB-операций (shared.database). operation ∈ {execute, fetch_one,
# fetch_all}; error_code — pgcode (SQLSTATE) или 'pool_exhausted'/'unknown'.
# scud_db_query_duration_seconds пишется только на success-пути — этот счётчик
# делает ошибки видимыми.
db_errors_total = Counter(
    "scud_db_errors_total",
    "DB operation errors by operation and SQLSTATE/error_code.",
    ["service", "operation", "error_code"],
)

# Сбои создания monthly-партиций history_api. label `table` ограничен именем
# партиционированной таблицы (сейчас только "sensor_telemetry"), кардинальность ≤ 5.
# Инкрементируется в retention._create_future_partitions при каждом except Exception.
partition_create_failures_total = Counter(
    "scud_partition_create_failures_total",
    "Total failures creating a monthly partition table by service and parent table.",
    ["service", "table"],
)


def inc_partition_create_failure(table: str) -> None:
    """Инкрементировать scud_partition_create_failures_total. Никогда не кидает."""
    try:
        partition_create_failures_total.labels(
            service=SERVICE_NAME, table=table
        ).inc()
    except Exception:
        logger.debug("Failed to inc scud_partition_create_failures_total", exc_info=True)


# Физическая безопасность (B3): tamper / intrusion / threshold / offline.
# Кардинальность ограничена: type ∈ SecurityEventType, severity ∈
# EventSeverity.
# Инкремент на enqueue-time в продьюсерах locker_api (см. history_producers).
security_events_total = Counter(
    "scud_security_events_total",
    "Total physical-security events enqueued by type and severity.",
    ["service", "type", "severity"],
)

# B2 — воронка входа. Все label-значения берутся из StrEnum'ов shared.enums
# (LoginOutcome/OtpStage/OtpOutcome), поэтому cardinality каждой серии ограничена
# сверху размером enum. Пароли/OTP/JWT/телефон/IP в метки НЕ попадают — только
# агрегированный outcome.
login_attempts_total = Counter(
    "scud_login_attempts_total",
    "Total login attempts by service/outcome.",
    ["service", "outcome"],
)

otp_total = Counter(
    "scud_otp_total",
    "Total OTP issue/verify events by service/stage/outcome.",
    ["service", "stage", "outcome"],
)

# Аномальные WS-разрывы (supersession). label `event` ограничен
# WsLifecycleEvent — cardinality = 1 (только supersession). normal_close намеренно
# исключён: on_close срабатывает на КАЖДЫЙ закрытый сокет и при подсчёте в нём
# double-count'ил бы прочие closes.
ws_events_total = Counter(
    "scud_ws_events_total",
    "WebSocket connection lifecycle events (abnormal disconnects) by service/event.",
    ["service", "event"],
)

# Жизненный цикл команд шкафа (главный СКУД-KPI). command_type = LockerCommand
# type value (open/close/reboot/config) — конечный набор, transition ∈
# CommandTransition. Перекрытий с scud_command_2fa_* нет.
command_lifecycle_total = Counter(
    "scud_command_lifecycle_total",
    "Total locker-command state transitions by command_type/transition.",
    ["service", "command_type", "transition"],
)

# queued→sent латентность очереди (время ожидания подключённого устройства /
# фонового процессора). created_at→sent_at в секундах.
command_queue_latency_seconds = Histogram(
    "scud_command_queue_latency_seconds",
    "Locker-command queue latency (queued→sent) in seconds.",
    ["service", "command_type"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0),
)

# sent→executed/failed латентность подтверждения устройства. sent_at→
# executed_at|failed_at в секундах.
command_ack_latency_seconds = Histogram(
    "scud_command_ack_latency_seconds",
    "Locker-command ack latency (sent→executed/failed) in seconds.",
    ["service", "command_type"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 300.0),
)

# ---------------------------------------------------------------------------
# MQTT / relay-board fleet (Этап 2). Все шесть — Gauge с единственным label
# `service` (заполняет только locker_api, который держит gmqtt-сессию). Брокерные
# *_total зеркалят кумулятивные счётчики $SYS/broker/# — это репортируемое брокером
# число, потому Gauge, а не Counter (мы не инкрементируем, а ОТРАЖАЕМ).
# ---------------------------------------------------------------------------

mqtt_connected = Gauge(
    "scud_mqtt_connected",
    "Whether the service holds a live MQTT broker session (1) or not (0).",
    ["service"],
)

mqtt_relays_online = Gauge(
    "scud_mqtt_relays_online",
    "Number of relay boards currently considered online by offline_monitor.",
    ["service"],
)

mqtt_broker_messages_received_total = Gauge(
    "scud_mqtt_broker_messages_received_total",
    "Broker-reported cumulative messages received ($SYS/broker/messages/received).",
    ["service"],
)

mqtt_broker_messages_sent_total = Gauge(
    "scud_mqtt_broker_messages_sent_total",
    "Broker-reported cumulative messages sent ($SYS/broker/messages/sent).",
    ["service"],
)

mqtt_broker_messages_dropped_total = Gauge(
    "scud_mqtt_broker_messages_dropped_total",
    "Broker-reported cumulative publish messages dropped ($SYS/broker/publish/messages/dropped).",
    ["service"],
)

mqtt_broker_clients_connected = Gauge(
    "scud_mqtt_broker_clients_connected",
    "Broker-reported currently connected clients ($SYS/broker/clients/connected).",
    ["service"],
)


class MqttBrokerStat(StrEnum):
    """Брокерные $SYS-статы (StrEnum: значение — строковый идентификатор, НЕ
    Gauge). Маппинг stat→gauge держит отдельный _STAT_GAUGES — так mypy строг
    без cast (R1)."""

    MESSAGES_RECEIVED = "messages_received"
    MESSAGES_SENT = "messages_sent"
    MESSAGES_DROPPED = "messages_dropped"
    CLIENTS_CONNECTED = "clients_connected"


_STAT_GAUGES: dict[MqttBrokerStat, Gauge] = {
    MqttBrokerStat.MESSAGES_RECEIVED: mqtt_broker_messages_received_total,
    MqttBrokerStat.MESSAGES_SENT: mqtt_broker_messages_sent_total,
    MqttBrokerStat.MESSAGES_DROPPED: mqtt_broker_messages_dropped_total,
    MqttBrokerStat.CLIENTS_CONNECTED: mqtt_broker_clients_connected,
}


# Исходы входящих MQTT-сообщений от релейных плат. outcome ∈
# RelayMessageOutcome — cardinality ограничена. Значения
# взаимоисключающие: ровно один инкремент на каждое сообщение.
mqtt_relay_messages_total = Counter(
    "scud_mqtt_relay_messages_total",
    "Total incoming MQTT relay-board messages by service/outcome.",
    ["service", "outcome"],
)


def record_relay_message(outcome: RelayMessageOutcome) -> None:
    """Инкрементировать scud_mqtt_relay_messages_total{service, outcome}. Никогда не кидает."""
    try:
        mqtt_relay_messages_total.labels(
            service=SERVICE_NAME, outcome=outcome.value
        ).inc()
    except Exception:
        logger.debug("Failed to record scud_mqtt_relay_messages_total", exc_info=True)


# disposition ∈ {new_series, new_frame, duplicate, reset} (locker_api FrameDisposition).
relay_photo_frames_total = Counter(
    "scud_relay_photo_frames_total",
    "Relay photo frames by series-registry disposition.",
    ["service", "disposition"],
)


def record_relay_photo_frame(disposition: str) -> None:
    """Инкрементировать scud_relay_photo_frames_total. Никогда не кидает."""
    try:
        relay_photo_frames_total.labels(service=SERVICE_NAME, disposition=disposition).inc()
    except Exception:
        logger.debug("Failed to record scud_relay_photo_frames_total", exc_info=True)


redis_up = Gauge(
    "scud_redis_up",
    "Whether the shared Redis is reachable from this service (1) or not (0).",
    ["service"],
)


def set_redis_up(up: bool) -> None:
    """Выставить scud_redis_up в 1/0. Никогда не кидает (зеркалит
    set_mqtt_connected) — scrape/hot-path не должны падать из-за метрик."""
    try:
        redis_up.labels(service=SERVICE_NAME).set(1.0 if up else 0.0)
    except Exception:
        logger.debug("Failed to set scud_redis_up", exc_info=True)


def record_ws_event(event: WsLifecycleEvent) -> None:
    """Инкрементировать scud_ws_events_total{service, event}. Никогда не кидает."""
    try:
        ws_events_total.labels(service=SERVICE_NAME, event=event.value).inc()
    except Exception:
        logger.debug("Failed to record scud_ws_events_total", exc_info=True)


def set_mqtt_connected(connected: bool) -> None:
    """Выставить scud_mqtt_connected в 1/0. Никогда не кидает (зеркалит
    update_pool_gauges) — scrape/hot-path не должны падать из-за метрик."""
    try:
        mqtt_connected.labels(service=SERVICE_NAME).set(1.0 if connected else 0.0)
    except Exception:
        logger.debug("Failed to set scud_mqtt_connected", exc_info=True)


def set_mqtt_relays_online(count: int) -> None:
    """Выставить scud_mqtt_relays_online = count. Никогда не кидает; нечисловой
    вход глотается (isinstance-чек как в update_pool_gauges)."""
    try:
        if not isinstance(count, int) or isinstance(count, bool):
            return
        mqtt_relays_online.labels(service=SERVICE_NAME).set(count)
    except Exception:
        logger.debug("Failed to set scud_mqtt_relays_online", exc_info=True)


command_pending_total = Gauge(
    "scud_command_pending_total",
    "Locker commands currently pending with retry budget remaining (sampled).",
    ["service"],
)


def set_command_pending(count: int) -> None:
    """Set scud_command_pending_total. Never raises; non-int/bool ignored."""
    try:
        if not isinstance(count, int) or isinstance(count, bool):
            return
        command_pending_total.labels(service=SERVICE_NAME).set(count)
    except Exception:
        logger.debug("Failed to set scud_command_pending_total", exc_info=True)


# Счётчик действительно необработанных исключений, достигших write_error
# (RuntimeError, QueryError, ValueError, неклассифицированные 500). НЕ растёт
# для UpstreamUnavailableError / AsyncPostgresError — это инфра-деградация,
# не баги кода. Один label `service` (6 значений, cardinality ограничена).
# Zabbix: change(scud_unhandled_errors_total) > 3 in 5m → алерт on-call.
unhandled_errors_total = Counter(
    "scud_unhandled_errors_total",
    "Total truly-unhandled exceptions reaching write_error (bugs, not infra-degradation).",
    ["service"],
)


def inc_unhandled_error() -> None:
    """Инкрементировать scud_unhandled_errors_total{service}. Никогда не кидает."""
    try:
        unhandled_errors_total.labels(service=SERVICE_NAME).inc()
    except Exception:
        logger.debug("Failed to inc scud_unhandled_errors_total", exc_info=True)


# Outbox dead-letter: события, запаркованные OutboundDispatcher'ом после
# исчерпания OUTBOUND_DISPATCHER_MAX_ATTEMPTS неуспешных доставок в history_api
# вместо бесконечного ретрая. channel ∈ {telemetry, media, security_event,
# command_history, ...} — конечный набор каналов, cardinality ограничена. Рост
# счётчика = доставка в history_api устойчиво ломается (poison-событие или
# затяжная недоступность); в норме ноль. Глубину очереди dead-letter отдаёт
# dbprobe (scud.dbprobe[pg,postgres_locker,outbox_deadletter]) — Counter здесь
# даёт rate появления, gauge на дашборде — накопленный объём.
outbound_dead_lettered_total = Counter(
    "scud_outbound_dead_lettered_total",
    "Total outbound history events parked in dead-letter after exhausting delivery attempts.",
    ["service", "channel"],
)


def record_outbound_dead_lettered(channel: str) -> None:
    """Инкрементировать scud_outbound_dead_lettered_total{service, channel}. Никогда не кидает."""
    try:
        outbound_dead_lettered_total.labels(
            service=SERVICE_NAME, channel=channel
        ).inc()
    except Exception:
        logger.debug("Failed to inc scud_outbound_dead_lettered_total", exc_info=True)


otp_delivery_total = Counter(
    "scud_otp_delivery_total",
    "Исходы доставки OTP по каналам (notification_api).",
    ["service", "channel", "outcome"],
)


def record_otp_delivery(channel: str, outcome: str) -> None:
    """Инкремент scud_otp_delivery_total{service,channel,outcome}. Никогда не кидает."""
    try:
        otp_delivery_total.labels(
            service=SERVICE_NAME, channel=str(channel), outcome=str(outcome),
        ).inc()
    except Exception:
        logger.debug("Failed to inc scud_otp_delivery_total", exc_info=True)


def set_mqtt_broker_stat(stat: MqttBrokerStat, value: float) -> None:
    """Отразить брокерный $SYS-счётчик в соответствующий gauge. Никогда не кидает;
    нечисловое value (нераспарсенный payload) глотается. $SYS-значения бывают
    дробными (rate/bytes) — потому value: float (D6 шлёт float)."""
    try:
        if isinstance(value, bool) or not isinstance(value, int | float):
            return
        gauge = _STAT_GAUGES.get(stat)
        if gauge is None:
            return
        gauge.labels(service=SERVICE_NAME).set(value)
    except Exception:
        logger.debug("Failed to set MQTT broker stat %r", stat, exc_info=True)


# ---------------------------------------------------------------------------
# Route normalization (anti-cardinality-explosion)
# ---------------------------------------------------------------------------

# UUID v4 (e.g. в ``/api/v1/locker/<uuid>/...``).
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.IGNORECASE,
)
# Числовые ID (``/api/v1/user/42`` → ``/api/v1/user/:id``). Привязка к ``/``
# границам, чтобы не зацепить, например, ``/api/v1/...`` (где число — часть
# версии, а не идентификатор).
_NUMERIC_RE = re.compile(r"/\d+(?=/|$)")


def _normalize_route(path: str) -> str:
    """Свести произвольный path к шаблону маршрута для label `route`.

    Tornado не отдаёт регулярку URLSpec'а напрямую без копания во
    внутренностях Application — пользоваться ``self.request.path`` как
    label-значением напрямую нельзя: каждое UUID/number сделает уникальную
    серию метрик и Prometheus захлебнётся (cardinality blow-up).

    Здесь мы делаем минимальную замену плейсхолдерами — точности достаточно,
    чтобы агрегировать запросы по эндпоинтам, а cardinality остаётся
    ограниченной числом реальных маршрутов.
    """
    path = _UUID_RE.sub(":id", path)
    return _NUMERIC_RE.sub("/:id", path)


# ---------------------------------------------------------------------------
# DB pool gauges — pull-based sampling registry
# ---------------------------------------------------------------------------

# Реестр info-провайдеров пулов: имя пула → callable, отдающий get_pool_info()-dict.
# Заполняется register_pool() из main.py каждого сервиса; читается
# update_pool_gauges() на каждый scrape /metrics.
_POOL_REGISTRY: dict[str, Callable[[], dict[str, Any]]] = {}

# get_pool_info()-ключ → gauge. waiters берётся из локального счётчика, in_use/idle
# из живого пула, max из конфига — всё семплируется в момент scrape.
_POOL_GAUGE_KEYS: tuple[tuple[str, Gauge], ...] = (
    ("in_use", db_pool_connections_in_use),
    ("idle", db_pool_connections_idle),
    ("waiters", db_pool_connections_waiters),
    ("maxconn", db_pool_connections_max),
)


def register_pool(name: str, info_provider: Callable[[], dict[str, Any]]) -> None:
    """Зарегистрировать info-провайдер пула под именем `name` (идемпотентно).

    Повторная регистрация под тем же именем перезаписывает запись — это
    нормально для рестартов/реинициализации в тестах.
    """
    _POOL_REGISTRY[name] = info_provider


def update_pool_gauges() -> None:
    """Семплировать все зарегистрированные пулы в gauge'и.

    Вызывается из MetricsHandler.get() ПЕРЕД generate_latest(). НИКОГДА не
    кидает — scrape не должен падать из-за метрик (зеркалит _record_db_metric).
    Закрытый пул отдаёт {'status': 'closed'} без числовых ключей → серия
    просто не обновляется (skip через .get() + isinstance-чек).
    """
    try:
        for name, provider in _POOL_REGISTRY.items():
            try:
                info = provider()
            except Exception:
                logger.debug(
                    "pool info provider %r raised", name, exc_info=True,
                )
                continue
            for key, gauge in _POOL_GAUGE_KEYS:
                value = info.get(key)
                if not isinstance(value, int) or isinstance(value, bool):
                    continue
                gauge.labels(service=SERVICE_NAME, pool=name).set(value)
    except Exception:
        logger.debug("Failed to update pool gauges", exc_info=True)


# ---------------------------------------------------------------------------
# Generic pull-based metric samplers (no-arg). Same philosophy as the pool
# registry above, but for gauges sampled from an in-process source at scrape
# time (e.g. scud_mqtt_connected reading MqttClient.is_connected). Each sampler
# is a zero-arg callable that sets its own gauges and MUST NOT raise — a
# misbehaving sampler is swallowed so /metrics never fails because of it.
# ---------------------------------------------------------------------------

_METRIC_SAMPLERS: list[Callable[[], None]] = []


def register_metric_sampler(sampler: Callable[[], None]) -> None:
    """Зарегистрировать zero-arg sampler, вызываемый на каждый scrape /metrics.

    Идемпотентности по содержимому нет (callable'ы не сравниваем) — caller
    регистрирует ровно один раз при старте сервиса.
    """
    _METRIC_SAMPLERS.append(sampler)


def run_metric_samplers() -> None:
    """Прогнать все зарегистрированные samplers перед generate_latest().

    Вызывается из MetricsHandler.get(). НИКОГДА не кидает — дефект одного
    sampler'а не должен валить весь /metrics-ответ (зеркалит update_pool_gauges).
    """
    for sampler in _METRIC_SAMPLERS:
        try:
            sampler()
        except Exception:
            logger.debug("metric sampler %r raised", sampler, exc_info=True)


__all__ = [
    "SERVICE_NAME",
    "http_requests_total",
    "http_request_duration_seconds",
    "db_query_duration_seconds",
    "db_pool_connections_in_use",
    "db_pool_connections_idle",
    "db_pool_connections_waiters",
    "db_pool_connections_max",
    "register_pool",
    "update_pool_gauges",
    "register_metric_sampler",
    "run_metric_samplers",
    "command_2fa_challenges_total",
    "command_2fa_delivery_total",
    "InterserviceStatusClass",
    "interservice_requests_total",
    "interservice_request_duration_seconds",
    "record_interservice_duration",
    "db_errors_total",
    "partition_create_failures_total",
    "inc_partition_create_failure",
    "security_events_total",
    "login_attempts_total",
    "otp_total",
    "ws_events_total",
    "record_ws_event",
    "command_lifecycle_total",
    "command_queue_latency_seconds",
    "command_ack_latency_seconds",
    "command_pending_total",
    "set_command_pending",
    "unhandled_errors_total",
    "inc_unhandled_error",
    "outbound_dead_lettered_total",
    "record_outbound_dead_lettered",
    "otp_delivery_total",
    "record_otp_delivery",
    "redis_up",
    "set_redis_up",
    "mqtt_connected",
    "mqtt_relays_online",
    "mqtt_broker_messages_received_total",
    "mqtt_broker_messages_sent_total",
    "mqtt_broker_messages_dropped_total",
    "mqtt_broker_clients_connected",
    "MqttBrokerStat",
    "set_mqtt_connected",
    "set_mqtt_relays_online",
    "set_mqtt_broker_stat",
    "mqtt_relay_messages_total",
    "record_relay_message",
    "relay_photo_frames_total",
    "record_relay_photo_frame",
    "generate_latest",
    "CONTENT_TYPE_LATEST",
    "_normalize_route",
]
