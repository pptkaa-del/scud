"""Async-обёртка над psycopg2 (AsyncPostgres) — фасад модуля shared.database.

Когезивные части вынесены в submodules и реэкспортируются здесь, чтобы публичная
поверхность ``shared.database.connection`` осталась прежней:
- ``config`` — ``PostgresConfig`` (frozen+slots dataclass с валидацией);
- ``helpers`` — чистые SQLSTATE-детекторы ``is_unique_violation`` /
  ``is_foreign_key_violation``;
- ``retry`` — чистая ретрай-семантика (``should_retry``, ``is_connection_broken``,
  ``RETRY_SQLSTATES``, pool-repoll константы) и ``record_db_metric``.

Сам класс ``AsyncPostgres`` (пул, async-обёртка, lifecycle) остаётся здесь.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
import contextlib
import logging
import os
import time
from types import TracebackType
from typing import Any, TypeVar

import psycopg2
import psycopg2.extensions
import psycopg2.extras
import psycopg2.pool
from psycopg2.pool import ThreadedConnectionPool
from tornado.ioloop import IOLoop

from . import retry
from .config import PostgresConfig
from .errors import (
    ConnectionPoolExhaustedError,
    QueryError,
    RetryExhaustedError,
    TransactionError,
    UnexpectedEmptyResultError,
)
from .helpers import is_foreign_key_violation, is_unique_violation
from .retry import RETRY_SQLSTATES, record_db_error_metric

# Реэкспорт для обратной совместимости: исторически эти имена жили в
# connection.py. Тесты и repository-код продолжают импортировать их отсюда.
__all__ = [
    "AsyncPostgres",
    "PostgresConfig",
    "RETRY_SQLSTATES",
    "is_foreign_key_violation",
    "is_unique_violation",
    "resolve_min_connections",
]

logger = logging.getLogger(__name__)

Params = Sequence[Any] | Mapping[str, Any]
Row = dict[str, Any]
_T = TypeVar("_T")

# error_code-значения для scud_db_errors_total, когда pgcode недоступен или
# отказ — на уровне пула (не SQLSTATE). Не голые литералы.
_POOL_EXHAUSTED_CODE = "pool_exhausted"
_UNKNOWN_ERROR_CODE = "unknown"


def resolve_min_connections(
    max_workers: int, min_connections: int | None, env_value: str | None
) -> int:
    """Resolve the pool's minconn with a WARM default.

    Priority: explicit arg > DB_MIN_CONNECTIONS env > max_workers.
    Defaulting to max_workers keeps the pool warm: psycopg2's
    ThreadedConnectionPool creates connections under its single global lock,
    so a cold pool (minconn << maxconn) serializes every getconn behind a
    connection handshake under load (lock convoy). Warming to max_workers
    means getconn reuses idle connections and never connects on the hot path.
    """
    if min_connections is not None:
        return min_connections
    if env_value is not None and env_value.strip() != "":
        return int(env_value)
    return max_workers


class AsyncPostgres:
    def __init__(
        self,
        config: PostgresConfig,
        *,
        max_workers: int | None = None,
        min_connections: int | None = None,
        retries: int = 3,
        retry_delay_sec: float = 0.2,
        retry_backoff_multiplier: float = 2.0,
        max_retry_delay_sec: float = 5.0,
        statement_timeout_ms: int | None = None,
        max_total_time_sec: float | None = None,
        pool_acquire_timeout_sec: float = 5.0,
    ) -> None:
        # Pool sizing — env-fallback, kwargs override.
        # Если caller передал явный max_workers/min_connections — он перебивает
        # env. Если None — max_workers читается из DB_MAX_WORKERS (дефолт 5), а
        # min_connections по умолчанию равен max_workers (ТЁПЛЫЙ пул — см.
        # resolve_min_connections: холодный пул minconn<<maxconn заставляет
        # psycopg2 создавать соединения под глобальным локом на hot-path).
        # Это позволяет операторам тюнить пул per-service через docker-compose
        # без правки кода.
        # Hot-services (locker_api 1000 WS, history_api telemetry) получают
        # повышенные значения через distinct env-переменные в compose
        # (DB_MAX_WORKERS_LOCKER, DB_MAX_WORKERS_HISTORY) — см. .env.example.
        if max_workers is None:
            max_workers = int(os.getenv("DB_MAX_WORKERS", "5"))
        min_connections = resolve_min_connections(
            max_workers, min_connections, os.getenv("DB_MIN_CONNECTIONS")
        )

        # Валидация параметров
        if max_workers <= 0:
            raise ValueError("max_workers must be > 0")
        if min_connections <= 0:
            raise ValueError("min_connections must be > 0")
        # retries — это число ПОПЫТОК выполнения (не «доп-ретраев сверх одной»):
        # range(1, retries+1) даёт ровно retries проходов. retries=0 → пустой
        # range → операция НИКОГДА не выполняется и сразу летит RetryExhaustedError
        # с last_exception=None (COR-31). Минимум осмысленного значения — 1.
        if retries < 1:
            raise ValueError("retries must be >= 1 (it is the number of attempts, not extra retries)")
        if retry_delay_sec < 0:
            raise ValueError("retry_delay_sec must be >= 0")
        if retry_backoff_multiplier < 1.0:
            raise ValueError("retry_backoff_multiplier must be >= 1.0")
        if pool_acquire_timeout_sec <= 0:
            raise ValueError("pool_acquire_timeout_sec must be > 0")

        self._config = config
        self._retries = retries
        self._retry_delay_sec = retry_delay_sec
        self._retry_backoff_multiplier = retry_backoff_multiplier
        self._max_retry_delay_sec = max_retry_delay_sec
        self._statement_timeout_ms = statement_timeout_ms
        self._max_total_time_sec = max_total_time_sec
        self._pool_acquire_timeout_sec = pool_acquire_timeout_sec

        # Ensure maxconn >= max_workers to prevent deadlocks
        maxconn = max(max_workers, min_connections)

        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="async-pg",
        )

        # Создание пула с дополнительными параметрами
        self._pool = ThreadedConnectionPool(
            minconn=min_connections,
            maxconn=maxconn,
            host=config.host,
            port=config.port,
            dbname=config.database,
            user=config.user,
            password=config.password,
            connect_timeout=config.connect_timeout_sec,
            application_name=config.application_name,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

        self._closed = False
        # Счётчик корутин, в данный момент ОЖИДАЮЩИХ освобождения соединения в
        # re-poll-ветке _acquire_and_run (pool-saturation telemetry). Семплируется
        # pull-based в get_pool_info(); инкремент/декремент только вокруг await,
        # не на hot-path getconn/putconn.
        self._pool_waiters: int = 0

    # ------------------------------------------------------------------
    # Connection management with context manager
    # ------------------------------------------------------------------

    @contextlib.contextmanager
    def _connection_context(
        self,
        *,
        read_only: bool = False
    ) -> Generator[psycopg2.extensions.connection, None, None]:
        """
        Безопасное управление соединениями с гарантированной очисткой.

        Args:
            read_only: Если True, начинает READ ONLY транзакцию

        Yields:
            psycopg2.connection: Соединение с активной транзакцией

        Raises:
            ConnectionPoolExhaustedError: Пул соединений истощен
        """
        if self._closed:
            raise RuntimeError("Connector is closed")

        conn = None
        try:
            # Получение соединения из пула.
            # ThreadedConnectionPool.getconn() при исчерпании пула кидает
            # psycopg2.pool.PoolError("connection pool exhausted") немедленно.
            # Преобразуем в наше исключение, чтобы async-обёртка могла
            # выждать pool_acquire_timeout_sec прежде чем сдаться.
            try:
                conn = self._pool.getconn()
            except psycopg2.pool.PoolError as exc:
                raise ConnectionPoolExhaustedError(str(exc)) from exc

            if conn is None:
                raise ConnectionPoolExhaustedError(
                    "Pool exhausted: no available connections."
                )

            # Применяем настройки сессии
            self._apply_transaction_settings(conn, read_only=read_only)

            yield conn

            # Успешное завершение - коммитим транзакцию
            conn.commit()

        except Exception as exc:
            # Любая ошибка - откатываем транзакцию
            if conn:
                with contextlib.suppress(psycopg2.Error):
                    conn.rollback()

            # Определяем нужно ли закрыть соединение. Это решение о ГИГИЕНЕ ПУЛА
            # (не о ретрае): сломанное/потенциально невалидное соединение нельзя
            # возвращать в пул, даже если саму операцию мы ретраить не будем
            # (ambiguous write, COR-10). Поэтому критерий шире _should_retry.
            should_close = isinstance(exc, psycopg2.Error) and self._is_connection_broken(exc)
            self._put_conn_safe(conn, close=should_close)
            raise

        else:
            self._put_conn_safe(conn, close=False)

    def _put_conn_safe(self, conn: psycopg2.extensions.connection | None, *, close: bool = False) -> None:
        """Безопасное возвращение соединения в пул"""
        if conn is None:
            return

        try:
            # Ensure no leaked transaction state is returned to the pool
            if not close and conn.status != psycopg2.extensions.STATUS_READY:
                try:
                    conn.rollback()
                except psycopg2.Error:
                    close = True
            self._pool.putconn(conn, close=close)
        except Exception as exc:
            logger.error("Failed to return connection to pool: %s", exc)

    # ------------------------------------------------------------------
    # Transaction settings and retry logic
    # ------------------------------------------------------------------

    def _apply_transaction_settings(
        self,
        conn: psycopg2.extensions.connection,
        *,
        read_only: bool,
    ) -> None:
        """Применяет настройки транзакции"""
        try:
            # Используем set_session вместо явного BEGIN для избежания вложенных транзакций
            # psycopg2 автоматически начнет транзакцию при первом execute()
            conn.set_session(
                readonly=read_only,
                autocommit=False
            )

            if self._statement_timeout_ms is not None:
                with conn.cursor() as cur:
                    cur.execute(
                        "SET LOCAL statement_timeout = %s",
                        (self._statement_timeout_ms,),
                    )

        except psycopg2.Error as exc:
            logger.error("Failed to apply transaction settings: %s", exc)
            raise TransactionError(f"Failed to configure transaction: {exc}") from exc

    def _is_connection_broken(self, exc: psycopg2.Error) -> bool:
        """True, если соединение после этой ошибки нельзя возвращать в пул.

        Тонкий метод-делегат к ``retry.is_connection_broken`` — оставлен на
        классе, т.к. тесты зовут его через ``__new__``-инстанс. Логика гигиены
        пула живёт в ``retry`` (чистая функция без self).
        """
        return retry.is_connection_broken(exc)

    def _should_retry(
        self,
        exc: psycopg2.Error,
        *,
        read_only: bool,
        idempotent: bool,
    ) -> bool:
        """Определяет можно ли повторить операцию после ошибки.

        Тонкий метод-делегат к ``retry.should_retry`` — оставлен на классе, т.к.
        тесты зовут ``db._should_retry(...)`` через ``__new__``-инстанс.
        Ретрай-семантика (COR-10) живёт в ``retry`` (чистая функция без self).
        """
        return retry.should_retry(exc, read_only=read_only, idempotent=idempotent)

    async def _acquire_and_run(
        self,
        operation_name: str,
        sync_fn: Callable[..., _T],
        args: tuple[Any, ...],
    ) -> _T:
        """Один проход выполнения sync_fn с time-budgeted ожиданием пула.

        Pool-exhaustion (psycopg2.pool.PoolError → ConnectionPoolExhaustedError)
        обрабатывается ЗДЕСЬ, отдельным циклом по бюджету
        ``pool_acquire_timeout_sec``: при занятом пуле повторно опрашиваем его
        короткими интервалами, пока соединение не освободится или не истечёт
        бюджет. Этот цикл НЕ списывает SQLSTATE-ретраи (COR-22) — ожидание
        свободного соединения и повтор после retryable-ошибки БД суть разные
        бюджеты.

        - Освободилось соединение в пределах бюджета → возвращаем результат.
        - Бюджет истёк, пул всё ещё занят → ConnectionPoolExhaustedError (НЕ
          RetryExhaustedError: это иной класс отказа, его и видит caller).
        - Любая НЕ-pool ошибка (psycopg2.Error и пр.) → пробрасываем сразу,
          чтобы её обработала attempt-counted SQLSTATE-петля в вызывающем коде.
        """
        pool_budget_start = time.monotonic()
        pool_delay = retry.POOL_REPOLL_START_SEC
        while True:
            try:
                return await IOLoop.current().run_in_executor(
                    self._executor, lambda: sync_fn(*args)
                )
            except ConnectionPoolExhaustedError:
                elapsed = time.monotonic() - pool_budget_start
                if elapsed >= self._pool_acquire_timeout_sec:
                    logger.warning(
                        "%s: pool acquire budget exhausted (%.2fs)",
                        operation_name, elapsed,
                    )
                    record_db_error_metric(operation_name, _POOL_EXHAUSTED_CODE)
                    raise
                logger.warning(
                    "%s: pool exhausted, re-polling in %.2fs (%.2fs/%.2fs budget)",
                    operation_name, pool_delay, elapsed, self._pool_acquire_timeout_sec,
                )
                # Метим себя как ожидающего соединение — только на время реального
                # await, чтобы pool-saturation gauge отражал давление на пул.
                # finally гарантирует декремент даже при CancelledError (иначе
                # счётчик утёк бы навсегда после отмены корутины).
                self._pool_waiters += 1
                try:
                    await asyncio.sleep(pool_delay)
                finally:
                    self._pool_waiters -= 1
                pool_delay = min(
                    pool_delay * retry.POOL_REPOLL_BACKOFF_MULTIPLIER,
                    retry.POOL_REPOLL_MAX_SEC,
                )

    async def _run_with_retry_async(
        self,
        operation_name: str,
        sync_fn: Callable[..., _T],
        *args: Any,
        read_only: bool,
        idempotent: bool,
    ) -> _T:
        """Async-обёртка над sync DB-операцией с retry и exponential backoff.

        ``read_only`` / ``idempotent`` НЕ передаются в sync_fn — они управляют
        только ретрай-решением (_should_retry, COR-10). Параметры самого
        запроса идут позиционно в ``*args``.

        Между попытками используется ``await asyncio.sleep``, что не блокирует
        ни event loop, ни рабочие потоки ThreadPoolExecutor — в отличие от
        time.sleep внутри thread, который удерживает рабочий поток на весь
        backoff и при насыщении очереди executor приводил бы к деградации
        всего сервиса.

        ДВА независимых бюджета (COR-22):
        - SQLSTATE-ретраи: attempt-counted цикл ``range(1, retries+1)`` ниже —
          каждый проход это реальная попытка выполнить запрос;
        - pool-acquire: бюджет времени ``pool_acquire_timeout_sec`` внутри
          ``_acquire_and_run`` — ожидание свободного соединения НЕ списывает
          SQLSTATE-попытки и при истечении даёт ConnectionPoolExhaustedError,
          а не RetryExhaustedError.
        """
        start_time = time.monotonic()
        last_exc: Exception | None = None
        current_delay = self._retry_delay_sec

        for attempt in range(1, self._retries + 1):
            try:
                result = await self._acquire_and_run(operation_name, sync_fn, args)
                if attempt > 1:
                    logger.info("%s succeeded on attempt %d", operation_name, attempt)
                # учитываем длительность только успешных запросов;
                # ретраи и pool-waits не загрязняют histogram.
                retry.record_db_metric(operation_name, time.monotonic() - start_time)
                return result

            except psycopg2.Error as exc:
                last_exc = exc
                # Каждая psycopg2.Error-попытка считается отдельно (per-attempt
                # at convergence) — ретраи под одним operation видны как рост
                # счётчика. error_code = SQLSTATE (pgcode) или 'unknown'.
                record_db_error_metric(
                    operation_name, getattr(exc, "pgcode", None) or _UNKNOWN_ERROR_CODE
                )

                if not self._should_retry(exc, read_only=read_only, idempotent=idempotent):
                    raise

                if self._max_total_time_sec is not None:
                    elapsed = time.monotonic() - start_time
                    if elapsed > self._max_total_time_sec:
                        raise RetryExhaustedError(
                            f"Max retry time exceeded for {operation_name}",
                            last_exception=exc,
                        ) from exc

                if attempt == self._retries:
                    break

                logger.warning(
                    "%s failed (attempt %d/%d), retrying in %.2fs",
                    operation_name, attempt, self._retries, current_delay,
                )

                await asyncio.sleep(current_delay)

                current_delay = min(
                    current_delay * self._retry_backoff_multiplier,
                    self._max_retry_delay_sec,
                )

        raise RetryExhaustedError(
            f"All {self._retries} retry attempts exhausted for {operation_name}",
            last_exception=last_exc,
        )

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def execute(
        self,
        query: str,
        params: Params | None = None,
        *,
        idempotent: bool = False,
    ) -> None:
        """
        Выполняет запрос без возврата данных (INSERT, UPDATE, DELETE, DDL)

        Args:
            query: SQL запрос
            params: Параметры запроса
            idempotent: Пометьте True, ТОЛЬКО если повторное выполнение запроса
                безопасно (нет риска дубликата): ON CONFLICT DO …, natural-key
                INSERT, CAS-UPDATE с WHERE status=... . По умолчанию False —
                при неоднозначном обрыве соединения (сервер мог уже применить
                statement/COMMIT, но ACK потерян) запрос НЕ ретраится, чтобы не
                создать молчаливый дубликат (COR-10). См. _should_retry.

        Raises:
            RetryExhaustedError: Все попытки выполнения исчерпаны
            ConnectionPoolExhaustedError: Пул соединений был занят дольше pool_acquire_timeout_sec
            psycopg2.Error: Ошибки PostgreSQL (для non-retryable ошибок и
                неоднозначного обрыва на write без idempotent=True)
        """
        await self._run_with_retry_async(
            "execute", self._execute_sync, query, params,
            read_only=False, idempotent=idempotent,
        )

    async def fetch_one(
        self,
        query: str,
        params: Params | None = None,
        *,
        read_only: bool = True,
        idempotent: bool = False,
    ) -> Row | None:
        """
        Выполняет запрос и возвращает первую строку результата

        Args:
            query: SQL запрос
            params: Параметры запроса
            read_only: True (по умолчанию) — READ ONLY транзакция, ретрай
                неоднозначного обрыва безопасен. False — write-side
                (INSERT … RETURNING / UPDATE … RETURNING).
            idempotent: Применимо только при read_only=False. True → ретраить
                неоднозначный обрыв даже для write (запрос идемпотентен:
                ON CONFLICT / natural-key / CAS). По умолчанию False (COR-10).

        Returns:
            Словарь с данными первой строки или None если результат пустой

        Raises:
            RetryExhaustedError: Все попытки выполнения исчерпаны
            ConnectionPoolExhaustedError: Пул соединений был занят дольше pool_acquire_timeout_sec
            psycopg2.Error: Ошибки PostgreSQL (для non-retryable ошибок)
        """
        return await self._run_with_retry_async(
            "fetch_one", self._fetch_one_sync, query, params, read_only,
            read_only=read_only, idempotent=idempotent,
        )

    async def fetch_one_strict(
        self,
        query: str,
        params: Params | None = None,
        *,
        read_only: bool = False,
        idempotent: bool = False,
    ) -> Row:
        """Вариант fetch_one для запросов, ОБЯЗАННЫХ вернуть строку.

        Применяется для INSERT … RETURNING и SELECT с гарантированным
        существованием. Если БД вернула 0 строк — выбрасывает
        UnexpectedEmptyResultError (нарушение инварианта вызывающего кода:
        обычно race-condition или сломанный WHERE). `read_only=False` по
        умолчанию: на практике все вызовы — write-side. Для read-only
        SELECT с гарантированным существованием передавайте `read_only=True`
        явно. `idempotent=True` — opt-in на ретрай неоднозначного обрыва для
        идемпотентного write (ON CONFLICT … RETURNING / CAS), см. COR-10.

        Raises:
            UnexpectedEmptyResultError: БД вернула пустой результат.
            RetryExhaustedError / ConnectionPoolExhaustedError / psycopg2.Error: см. fetch_one.
        """
        row = await self.fetch_one(query, params, read_only=read_only, idempotent=idempotent)
        if row is None:
            # Query попадает в exc.query для structured-логирования; params
            # НЕ включаем — в них могут быть password_hash / OTP / токены.
            raise UnexpectedEmptyResultError(
                "fetch_one_strict: expected single row, got NULL",
                query=query,
            )
        return row

    async def fetch_all(
        self,
        query: str,
        params: Params | None = None,
        *,
        read_only: bool = True,
        idempotent: bool = False,
    ) -> list[Row]:
        """
        Выполняет запрос и возвращает все строки результата

        Args:
            query: SQL запрос
            params: Параметры запроса
            read_only: Если True (по умолчанию) — READ ONLY транзакция, как у
                всех исторических read-side вызовов. Передавайте False для
                write-side запросов с возвратом строк (UPDATE/DELETE …
                RETURNING), которым нужна обычная read-write транзакция.
            idempotent: Применимо только при read_only=False. True → ретраить
                неоднозначный обрыв даже для write (запрос идемпотентен). По
                умолчанию False (COR-10).

        Returns:
            Список словарей с данными строк

        Raises:
            RetryExhaustedError: Все попытки выполнения исчерпаны
            ConnectionPoolExhaustedError: Пул соединений был занят дольше pool_acquire_timeout_sec
            psycopg2.Error: Ошибки PostgreSQL (для non-retryable ошибок)
        """
        return await self._run_with_retry_async(
            "fetch_all", self._fetch_all_sync, query, params, read_only,
            read_only=read_only, idempotent=idempotent,
        )

    # ------------------------------------------------------------------
    # Sync implementations using connection context manager
    # ------------------------------------------------------------------

    def _execute_sync(self, query: str, params: Params | None) -> None:
        """Один проход выполнения запроса без возврата данных. Retry — на async-уровне."""
        with self._connection_context(read_only=False) as conn, conn.cursor() as cur:
            cur.execute(query, params)

    def _fetch_one_sync(
        self,
        query: str,
        params: Params | None,
        read_only: bool,
    ) -> Row | None:
        """Один проход выполнения запроса с возвратом одной строки."""
        with self._connection_context(read_only=read_only) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            row = cur.fetchone()
            if row is None or isinstance(row, dict):
                return row
            raise QueryError(f"fetchone returned unexpected type {type(row).__name__}")

    def _fetch_all_sync(
        self,
        query: str,
        params: Params | None,
        read_only: bool,
    ) -> list[Row]:
        """Один проход выполнения запроса с возвратом всех строк."""
        with self._connection_context(read_only=read_only) as conn, conn.cursor() as cur:
            cur.execute(query, params)
            return list(cur.fetchall())

    # ------------------------------------------------------------------
    # Lifecycle management
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AsyncPostgres:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """
        Graceful shutdown коннектора.
        Должен вызываться при завершении приложения.
        """
        if self._closed:
            return

        logger.info("Shutting down AsyncPostgres connector")

        try:
            self._executor.shutdown(wait=True)
        except Exception as exc:
            logger.error("Error shutting down thread executor: %s", exc)

        try:
            self._pool.closeall()
        except Exception as exc:
            logger.error("Error closing connection pool: %s", exc)

        self._closed = True

    @property
    def is_closed(self) -> bool:
        """Возвращает True если коннектор закрыт"""
        return self._closed

    @property
    def pool_name(self) -> str:
        """Имя пула для label `pool` в метриках — имя БД из конфига.

        Публичный аксессор, чтобы вызывающий код (main.py register_pool) не лез
        в приватный _config.
        """
        return self._config.database

    # ------------------------------------------------------------------
    # Utility methods for debugging
    # ------------------------------------------------------------------

    def get_pool_info(self) -> dict[str, Any]:
        """
        Возвращает информацию о состоянии пула соединений

        Returns:
            Словарь с информацией о пуле
        """
        if self._closed:
            return {"status": "closed"}

        try:
            # psycopg2 2.9.x: _used — dict {conn_key: conn} занятых соединений,
            # _pool — list свободных. getattr-with-default держит это безопасным,
            # если внутренние поля драйвера изменятся.
            pool_stats: dict[str, Any] = {
                "minconn": getattr(self._pool, '_minconn', 'unknown'),
                "maxconn": getattr(self._pool, '_maxconn', 'unknown'),
                "closed": getattr(self._pool, '_closed', 'unknown'),
                "in_use": len(getattr(self._pool, '_used', {})),
                "idle": len(getattr(self._pool, '_pool', [])),
                "waiters": self._pool_waiters,
                "database": self._config.database,
            }

            if hasattr(self._pool, '_pool'):
                pool_stats["pool_size"] = len(self._pool._pool)

            return pool_stats

        except Exception as exc:
            return {"error": str(exc)}
