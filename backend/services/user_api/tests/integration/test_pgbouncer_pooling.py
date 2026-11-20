"""Интеграционный тест транзакционного пулинга PgBouncer (pgbouncer_user).

Проверяет, что путь приложение → PgBouncer (transaction mode) → postgres
работает с psycopg2 и реально ограничивает число серверных соединений:

(a) round-trip: AsyncPostgres подключается к pgbouncer_user:6432 и выполняет
    реальный запрос. Доказывает, что transaction-режим + ignore_startup_parameters
    переваривают application_name, который psycopg2 шлёт в startup-пакете (без
    игнора pgbouncer отверг бы подключение с "unsupported startup parameter").

(b) cap: открываем N > default_pool_size одновременных медленных запросов через
    пулер и через отдельное подключение считаем в pg_stat_activity, сколько
    серверных backend'ов реально выполняют наш маркерный запрос ОДНОВРЕМЕННО.
    PgBouncer мультиплексирует клиентов на фикс-пул, поэтому одновременно
    активных серверных соединений не больше default_pool_size + reserve_pool_size.

Сидирование данных не нужно — тест работает с системными представлениями и
pg_sleep. Подключение идёт к pgbouncer_user (env PGBOUNCER_HOST/PGBOUNCER_PORT),
а НЕ к postgres напрямую (в отличие от conftest, который сидирует через psycopg2).
"""
import asyncio
import os

from shared.database import AsyncPostgres, PostgresConfig

# --- Константы конфигурации (env проставляет docker-compose user_api_tests) ---
_PGBOUNCER_HOST = os.getenv("PGBOUNCER_HOST", "pgbouncer_user")
_PGBOUNCER_PORT = int(os.getenv("PGBOUNCER_PORT", "6432"))
_DB_USER = os.getenv("DB_USER", "admin")
_DB_PASSWORD = os.getenv("DB_PASSWORD", "password")
_DB_NAME = os.getenv("DB_NAME", "scud_users")

# Размер серверного пула pgbouncer_user (compose: PGB_DEFAULT_POOL_SIZE_USER).
_DEFAULT_POOL_SIZE = int(os.getenv("PGB_DEFAULT_POOL_SIZE_USER", "15"))
# reserve_pool_size в шаблоне (compose: PGB_RESERVE_POOL_SIZE).
_RESERVE_POOL_SIZE = int(os.getenv("PGB_RESERVE_POOL_SIZE", "5"))

# Application name, который сервис передаёт при подключении. Намеренно
# нестандартный — именно его pgbouncer обязан проигнорировать в startup-пакете.
_APP_NAME = "user_api_pgbouncer_test"

# Маркер в тексте медленного запроса — по нему считаем активные backend'ы в
# pg_stat_activity (исключая сам считающий запрос).
_PROBE_MARKER = "pgbouncer_pool_probe_marker"

# Сколько одновременных медленных запросов открыть: заведомо больше пула, чтобы
# часть клиентов гарантированно встала в очередь на стороне pgbouncer.
_CONCURRENCY = _DEFAULT_POOL_SIZE + _RESERVE_POOL_SIZE + 10
# Длительность медленного запроса (с) — должно хватить, чтобы все задачи успели
# дойти до сервера, и чтобы счётчик активных backend'ов был снят в окне.
_SLEEP_SECONDS = 3.0
# Максимум одновременно активных серверных backend'ов = пул + reserve.
_MAX_EXPECTED_SERVER_BACKENDS = _DEFAULT_POOL_SIZE + _RESERVE_POOL_SIZE


def _make_db(*, app_name: str = _APP_NAME, max_workers: int = 1) -> AsyncPostgres:
    """AsyncPostgres, подключённый к pgbouncer_user:6432 (а не к postgres)."""
    config = PostgresConfig(
        host=_PGBOUNCER_HOST,
        port=_PGBOUNCER_PORT,
        database=_DB_NAME,
        user=_DB_USER,
        password=_DB_PASSWORD,
        application_name=app_name,
    )
    return AsyncPostgres(config, max_workers=max_workers, min_connections=1)


def test_round_trip_through_pgbouncer():
    """(a) Реальный запрос проходит через pgbouncer в transaction-режиме.

    Успех доказывает, что ignore_startup_parameters покрывает application_name,
    который psycopg2 кладёт в startup-пакет (иначе подключение упало бы с
    "unsupported startup parameter").
    """
    async def _run() -> None:
        db = _make_db()
        try:
            row = await db.fetch_one("SELECT 42 AS answer", read_only=True)
            assert row is not None
            assert row["answer"] == 42
        finally:
            db.close()

    asyncio.run(_run())


def test_server_connections_capped_at_pool_size():
    """(b) Одновременных серверных backend'ов не больше default_pool_size+reserve.

    Открываем _CONCURRENCY медленных запросов через пулер и параллельно считаем
    в pg_stat_activity backend'ы, реально выполняющие наш маркерный запрос.
    PgBouncer держит на сервере не больше пула — остальные клиенты ждут.
    """
    async def _run() -> int:
        # Пул-нагрузка: каждый из _CONCURRENCY клиентов держит соединение к
        # pgbouncer на время pg_sleep. У каждого свой однопоточный AsyncPostgres,
        # чтобы клиентских коннектов было ровно _CONCURRENCY.
        load_dbs = [_make_db(app_name=f"{_APP_NAME}_load_{i}") for i in range(_CONCURRENCY)]
        observer = _make_db(app_name=f"{_APP_NAME}_observer")

        slow_query = f"SELECT pg_sleep(%s) /* {_PROBE_MARKER} */"

        async def _slow(db: AsyncPostgres) -> None:
            await db.fetch_one(slow_query, (_SLEEP_SECONDS,), read_only=True)

        try:
            tasks = [asyncio.create_task(_slow(db)) for db in load_dbs]
            # Дать клиентам время дойти до сервера и насытить пул.
            await asyncio.sleep(_SLEEP_SECONDS / 2.0)

            # Считаем серверные backend'ы, выполняющие маркерный запрос ИМЕННО
            # сейчас (state='active'), исключая считающий запрос. Маркер в
            # комментарии исходного pg_sleep попадает в pg_stat_activity.query.
            count_row = await observer.fetch_one(
                """
                SELECT count(*) AS active_backends
                FROM pg_stat_activity
                WHERE datname = %s
                  AND state = 'active'
                  AND query LIKE %s
                  AND query NOT LIKE %s
                """,
                (_DB_NAME, f"%{_PROBE_MARKER}%", "%pg_stat_activity%"),
                read_only=True,
            )
            assert count_row is not None
            active_backends = int(count_row["active_backends"])

            await asyncio.gather(*tasks)
            return active_backends
        finally:
            observer.close()
            for db in load_dbs:
                db.close()

    active_backends = asyncio.run(_run())

    # Главная проверка: одновременных серверных backend'ов не больше пула+reserve,
    # хотя клиентов было _CONCURRENCY (заведомо больше).
    assert active_backends <= _MAX_EXPECTED_SERVER_BACKENDS, (
        f"pgbouncer не ограничил серверные соединения: активных backend'ов "
        f"{active_backends} > пул+reserve {_MAX_EXPECTED_SERVER_BACKENDS} "
        f"(клиентов было {_CONCURRENCY})"
    )
    # Sanity: нагрузка реально дошла до сервера (иначе тест ничего не проверяет).
    assert active_backends >= 1, (
        "ни один маркерный запрос не виден в pg_stat_activity — нагрузка не дошла "
        "до сервера, тест не валиден"
    )
