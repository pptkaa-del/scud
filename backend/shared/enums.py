"""Cross-service бизнес-enum'ы.

Здесь живут только enum'ы, используемые более чем одним сервисом.
Локальные enum'ы (DoorState, NotificationType, …) остаются в моделях своего сервиса.
"""
from enum import IntEnum, StrEnum


class UserState(StrEnum):
    """Состояние пользователя. users.state. Soft-delete через DEACTIVATED."""
    ACTIVE = "active"
    DEACTIVATED = "deactivated"
    DELETED = "deleted"


class UserRole(StrEnum):
    """Роль пользователя в СКУД (см. Architecture.md → Users.md)."""
    WORKER = "worker"
    OPERATOR = "operator"
    ADMINISTRATOR = "administrator"


class SecurityEventType(StrEnum):
    """Тип события безопасности. Контракт с history_api/internal/events/security.

    `COMMAND_2FA_*` — аудит step-up подтверждения привилегированных команд
    шкафа. issued/verified/failed публикует auth_api (он владеет lifecycle
    challenge'а), mismatch — locker_api (он сверяет URL и пермишены).
    `COMMAND_LATE_EXECUTION` публикует locker_api на SUCCESS-rep по уже
    терминальной команде.
    """
    THRESHOLD_VIOLATION = "threshold_violation"
    INTRUSION = "intrusion"
    OFFLINE = "offline"
    DOOR_TAMPER = "door_tamper"
    COMMAND_2FA_ISSUED = "command_2fa_issued"
    COMMAND_2FA_VERIFIED = "command_2fa_verified"
    COMMAND_2FA_FAILED = "command_2fa_failed"
    COMMAND_2FA_MISMATCH = "command_2fa_mismatch"
    COMMAND_LATE_EXECUTION = "command_late_execution"


class EventSeverity(StrEnum):
    """Severity для security_events. Контракт с history_api.

    `INFO`/`WARNING`/`ALERT` добавлены для аудита step-up 2FA: успешный
    issue/verify (info), отказ verify (warning), несовпадение scope/URL
    (alert — потенциальная атака). Старые `LOW`/`MEDIUM`/`HIGH`/`CRITICAL`
    продолжают использоваться датчиками.
    """
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    INFO = "info"
    WARNING = "warning"
    ALERT = "alert"


class CommandStatus(StrEnum):
    """Статус выполнения команды шкафа.

    DB-контракт: PG enum ``command_status`` (locker_commands.status,
    locker_api 001_init.sql) и history ``command_history.status`` (VARCHAR).
    Порядок членов соответствует PG enum — не менять.
    """
    PENDING = "pending"
    SENT = "sent"
    DELIVERED = "delivered"
    EXECUTED = "executed"
    FAILED = "failed"


class CommandType(StrEnum):
    """Тип команды шкафа.

    DB-контракт: PG enum ``command_type`` (locker_commands.type,
    locker_api 001_init.sql) и history ``command_history.command_type``
    (VARCHAR). Используется матрицей прав в shared.access_control.commands.
    """
    OPEN = "open"
    CLOSE = "close"
    REBOOT = "reboot"
    CONFIG = "config"
    RELAY_ON = "relay_on"
    RELAY_OFF = "relay_off"
    RELAY_RESTART = "relay_restart"


class CommandTransition(StrEnum):
    """Переход жизненного цикла команды шкафа для метрик Prometheus.

    Используется в label `transition` метрики ``scud_command_lifecycle_total``.
    Не является DB-контрактом — только для instrumentation.
    """
    QUEUED = "queued"
    SENT = "sent"
    SEND_FAILED = "send_failed"
    EXECUTED = "executed"
    FAILED = "failed"
    ACK_TIMEOUT_RETRY = "ack_timeout_retry"
    ACK_TIMEOUT_FAILED = "ack_timeout_failed"
    TTL_EXPIRED = "ttl_expired"


class LoginOutcome(StrEnum):
    """Исход попытки входа для scud_login_attempts_total."""

    RATE_LIMITED = "rate_limited"
    UPSTREAM_UNAVAILABLE = "upstream_unavailable"
    BAD_CREDENTIAL = "bad_credential"
    SUCCESS_NO_2FA = "success_no_2fa"
    SUCCESS_2FA = "success_2fa"


class OtpStage(StrEnum):
    """Стадия жизненного цикла OTP для scud_otp_total."""

    ISSUE = "issue"
    VERIFY = "verify"


class OtpOutcome(StrEnum):
    """Исход стадии OTP для scud_otp_total."""

    SENT = "sent"
    QUEUED = "queued"
    DELIVERY_UNAVAILABLE = "delivery_unavailable"
    RATE_LIMITED = "rate_limited"
    NO_ACTIVE_OTP = "no_active_otp"
    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    CODE_MISMATCH = "code_mismatch"
    SUCCESS = "success"


class OtpDeliveryOutcome(StrEnum):
    """Исход доставки OTP по каналу для scud_otp_delivery_total."""

    SENT = "sent"
    QUEUED = "queued"
    FALLBACK_SENT = "fallback_sent"
    UNAVAILABLE = "unavailable"


class NotificationChannel(StrEnum):
    """Канал доставки исходящих сообщений (OTP и пр.)."""

    MAX = "max"
    SMS = "sms"


class InterserviceOutcome(StrEnum):
    """Исход межсервисного HTTP-вызова (shared.http.RetryHTTPClient).

    Метка `outcome` метрики scud_interservice_requests_total. Семантика
    подсчёта: RETRY инкрементируется на КАЖДОЙ ретраябельной попытке;
    SUCCESS/ERROR/EXHAUSTED — ровно один раз на вызов fetch() в точке
    финального исхода ветки.
    """

    SUCCESS = "success"
    RETRY = "retry"
    ERROR = "error"
    EXHAUSTED = "exhausted"


class WsCloseCode(IntEnum):
    """WebSocket application-level close-коды (диапазон 4000–4999, RFC 6455).

    IntEnum — члены равны своим int-значениям, поэтому можно передавать
    непосредственно в Tornado ``self.close(code=WsCloseCode.SUPERSEDED)``
    без приведения.
    """

    SUPERSEDED = 4000


class WsLifecycleEvent(StrEnum):
    """Тип аномального WS-разрыва для метрики scud_ws_events_total (label `event`).

    Значение намеренно совпадает со строкой REASON_* в
    ``offline_producer.py``, чтобы таксономии согласовывались.
    """

    SUPERSESSION = "supersession"


class InterserviceStatusClass(StrEnum):
    """Класс HTTP-статуса межсервисного вызова (label `status_class`).

    Используется в label `status_class` метрики
    ``scud_interservice_request_duration_seconds``. Значения соответствуют
    терминальным исходам ветки RetryHTTPClient.fetch().
    """

    TWO_XX = "2xx"
    FOUR_XX = "4xx"
    FIVE_XX = "5xx"
    NETWORK_ERROR = "network_error"


class RelayMessageOutcome(StrEnum):
    """Исход обработки входящего MQTT-сообщения от релейной платы.

    Используется в label `outcome` метрики ``scud_mqtt_relay_messages_total``.
    Значения взаимоисключающие: ровно одно инкрементируется на каждое сообщение.
    """

    PROCESSED = "processed"
    DROPPED_MALFORMED = "dropped_malformed"
    DROPPED_UNKNOWN_RELAY = "dropped_unknown_relay"
    HANDLER_ERROR = "handler_error"
    DROPPED_OVERLOAD = "dropped_overload"
    STALE_REP = "stale_rep"
    DROPPED_RATE_LIMITED = "dropped_rate_limited"


class RelayPhotoMetaKey(StrEnum):
    """Ключи ``locker_media.metadata`` кадра relay-фото.

    Пишет locker_api, читает history_api (сортировка и отсев legacy-окна в SQL).
    """

    RELAY_EVENT_ID = "relay_event_id"
    PHOTO_ID = "photo_id"
    PHOTOS_CNT = "photos_cnt"
    SERIES_UID = "series_uid"
