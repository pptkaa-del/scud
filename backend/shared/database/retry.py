"""Чистая ретрай-семантика и backoff-параметры DB-операций (COR-10, COR-22).

Функции здесь не держат состояния (нет ``self``): они классифицируют исключения
psycopg2 для решений о ретрае и гигиене пула, а ``record_db_metric`` пишет
длительность успешных запросов в Prometheus-histogram. Async-обёртка и сам пул
живут в ``connection.py`` — этот модуль не импортирует connection и не содержит
async-кода, чтобы граф импортов оставался ацикличным.
"""

from __future__ import annotations

import logging

import psycopg2

logger = logging.getLogger(__name__)

# Prometheus-метрика длительности DB-запросов. Защищена try/except
# на случай, если python3-prometheus-client не установлен (старый билд).
try:
    from ..metrics import (
        SERVICE_NAME as _METRICS_SERVICE_NAME,
        db_errors_total,
        db_query_duration_seconds,
    )
    _METRICS_AVAILABLE = True
except Exception:
    _METRICS_AVAILABLE = False

# Pool-acquire re-poll: при исчерпании пула ждём освобождения соединения
# короткими интервалами (экспоненциальный рост от _START до _MAX), пока не
# истечёт бюджет pool_acquire_timeout_sec. Это ОТДЕЛЬНЫЙ от SQLSTATE-ретраев
# цикл — ожидание пула НЕ списывает попытки, отведённые на retryable-ошибки
# БД (COR-22).
POOL_REPOLL_START_SEC = 0.05
POOL_REPOLL_MAX_SEC = 0.5
POOL_REPOLL_BACKOFF_MULTIPLIER = 2.0

# Расширенные SQLSTATE codes для retry
RETRY_SQLSTATES: frozenset[str] = frozenset({
    # Connection errors
    "08000",  # connection_exception
    "08003",  # connection_does_not_exist
    "08006",  # connection_failure
    "08001",  # sqlclient_unable_to_establish_sqlconnection
    "08004",  # sqlserver_rejected_establishment_of_sqlconnection
    "08P01",  # protocol_violation

    # Resource errors
    "53000",  # insufficient_resources
    "53100",  # disk_full
    "53200",  # out_of_memory
    "53300",  # too_many_connections
    "53400",  # configuration_limit_exceeded

    # Server shutdown errors
    "57P01",  # admin_shutdown
    "57P02",  # crash_shutdown
    "57P03",  # cannot_connect_now
    "57P04",  # database_dropped
    "57P05",  # idle_session_timeout
})


def record_db_metric(operation_name: str, duration_sec: float) -> None:
    """Записать длительность DB-запроса в Prometheus-histogram.

    Никогда не кидает — production-flow не должен падать из-за метрик.
    Если prometheus_client отсутствует — no-op.
    """
    if not _METRICS_AVAILABLE:
        return
    try:
        db_query_duration_seconds.labels(
            service=_METRICS_SERVICE_NAME,
            operation=operation_name,
        ).observe(duration_sec)
    except Exception:
        logger.debug("Failed to record DB metric", exc_info=True)


def record_db_error_metric(operation_name: str, error_code: str) -> None:
    """Инкремент scud_db_errors_total{operation, error_code}.

    Зеркалит record_db_metric: никогда не кидает (production-flow не должен
    падать из-за метрик), no-op без prometheus_client. error_code — это
    psycopg2 pgcode (SQLSTATE), 'pool_exhausted' или 'unknown'.
    """
    if not _METRICS_AVAILABLE:
        return
    try:
        db_errors_total.labels(
            service=_METRICS_SERVICE_NAME,
            operation=operation_name,
            error_code=error_code,
        ).inc()
    except Exception:
        logger.debug("Failed to record DB error metric", exc_info=True)


def is_connection_broken(exc: psycopg2.Error) -> bool:
    """True, если соединение после этой ошибки нельзя возвращать в пул.

    Решение о ГИГИЕНЕ ПУЛА, отделённое от ретрай-решения (should_retry):
    connection-establishment/shutdown SQLSTATE'ы и безкодовый обрыв
    (OperationalError/InterfaceError без pgcode) означают, что соединение
    потенциально мертво — закрываем его, чтобы пул выдал свежее. Здесь нет
    различия read/write: даже неоднозначный write, который мы НЕ ретраим,
    не должен оставить битый сокет в пуле.
    """
    pgcode = getattr(exc, 'pgcode', None)
    if pgcode in RETRY_SQLSTATES:
        return True
    return bool(
        pgcode is None
        and isinstance(exc, psycopg2.OperationalError | psycopg2.InterfaceError)
    )


def should_retry(
    exc: psycopg2.Error,
    *,
    read_only: bool,
    idempotent: bool,
) -> bool:
    """Определяет можно ли повторить операцию после ошибки.

    Семантика ретрая разделена по фазе/безопасности повтора (COR-10):

    - Ошибки с явным retryable SQLSTATE (RETRY_SQLSTATES) — это
      connection-establishment / resource / shutdown-коды; повтор берёт
      свежее соединение из пула и безопасен независимо от read/write.

    - Ошибки БЕЗ pgcode (``pgcode is None``) от OperationalError/
      InterfaceError — обрыв соединения, который мог прийти ПОСЛЕ того, как
      сервер уже применил statement или COMMIT, но ACK потерян. Это
      НЕОДНОЗНАЧНЫЙ случай:
        * read-only (SELECT) — повтор безопасен (нет side-effect) → True;
        * write (read_only=False) — слепой повтор INSERT/UPDATE на таблице
          без unique-constraint даёт молчаливый дубликат (порча
          audit-trail). Ретраим ТОЛЬКО если caller явно пометил операцию
          идемпотентной (idempotent=True: ON CONFLICT / natural-key /
          CAS UPDATE WHERE status=...). Иначе → False: лучше поднять
          явную ошибку, чем тихо продублировать запись.

    Прагматика: psycopg2 не даёт надёжно отличить acquisition-фазу
    (statement ещё не выполнялся, повтор безопасен) от execution/commit-
    фазы для безкодового обрыва, поэтому для writes по умолчанию НЕ
    ретраим неоднозначный случай, а opt-in отдаём caller'у.
    """
    pgcode = getattr(exc, 'pgcode', None)
    if pgcode in RETRY_SQLSTATES:
        return True
    # Connection-loss errors (server closed mid-statement, network drop, etc.)
    # arrive without a pgcode because no SQLSTATE was received from the server.
    is_ambiguous_conn_loss = pgcode is None and isinstance(
        exc, psycopg2.OperationalError | psycopg2.InterfaceError,
    )
    if not is_ambiguous_conn_loss:
        return False
    # read → нет side-effect, ретрай свободен; write → только idempotent opt-in.
    return read_only or idempotent
