"""Real-DB integration тесты OutboundRepository.fetch_pending — атомарный
claim+lease через `FOR UPDATE SKIP LOCKED`.

Покрывают то, что unit-тесты с моком db.fetch_all не могут: что Postgres
действительно НЕ отдаёт одну и ту же строку двум ОДНОВРЕМЕННО открытым
транзакциям (`FOR UPDATE SKIP LOCKED`, проверяется через два сырых
psycopg2-соединения в отдельных потоках с `threading.Barrier`) и что lease
(сдвиг next_attempt_at) переживает commit, поэтому немедленная повторная
выборка не возвращает только что заявленные строки.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import os
import threading
import uuid

import psycopg2

from app.locker_data.db.repositories.outbound import OutboundRepository
from shared.database import AsyncPostgres, PostgresConfig

# Количество строк, заявляемых в тесте конкурентного claim'а: достаточно много,
# чтобы оба потока гарантированно что-то забрали при разбиении пополам.
_CONCURRENT_ROWS = 20
# Тайм-аут барьера/потоков: с SKIP LOCKED ни один поток не блокируется на
# чужих строках, поэтому секунды более чем достаточно. Если SKIP LOCKED убрать,
# один поток упрётся в lock первого и провисит дольше — тест свалится по
# тайм-ауту барьера, что и есть искомая регрессионная сигнализация.
_BARRIER_TIMEOUT_SECONDS = 30

# Канал/endpoint для тестовых строк — значения не важны для логики выборки,
# фиксируем константами, чтобы не плодить строковые литералы.
_TEST_CHANNEL = "telemetry"
_TEST_ENDPOINT = "/internal/api/v1/telemetry"


def _db_config() -> PostgresConfig:
    return PostgresConfig(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        database=os.getenv("DB_NAME", "scud"),
    )


def _sync_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        user=os.getenv("DB_USER", "admin"),
        password=os.getenv("DB_PASSWORD", "password"),
        dbname=os.getenv("DB_NAME", "scud"),
    )


def _insert_event(
    conn,
    tag: str,
    *,
    delay_seconds: int = 0,
    delivered: bool = False,
) -> int:
    """Создать строку outbound_history_events. tag попадает в payload, чтобы
    отличать строки конкретного теста от чужих."""
    payload = f'{{"tag": "{tag}"}}'
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO outbound_history_events
                (channel, endpoint, payload, next_attempt_at, delivered_at)
            VALUES (
                %s, %s, %s::jsonb,
                now() + make_interval(secs => %s),
                CASE WHEN %s THEN now() ELSE NULL END
            )
            RETURNING id
            """,
            (_TEST_CHANNEL, _TEST_ENDPOINT, payload, delay_seconds, delivered),
        )
        return int(cur.fetchone()[0])


def _next_attempt_at(conn, event_id: int) -> datetime:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT next_attempt_at FROM outbound_history_events WHERE id = %s",
            (event_id,),
        )
        return cur.fetchone()[0]


def _delete_events(conn, ids: list[int]) -> None:
    if not ids:
        return
    with conn.cursor() as cur:
        cur.execute(
            "DELETE FROM outbound_history_events WHERE id = ANY(%s)", (ids,)
        )


def test_ready_rows_returned_in_id_order():
    """Базовое поведение: готовые строки (delivered_at IS NULL,
    next_attempt_at <= now()) возвращаются в порядке id."""
    tag = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    ids: list[int] = []
    try:
        ids = [_insert_event(conn, tag) for _ in range(3)]
        conn.commit()

        async def _run() -> list[int]:
            db = AsyncPostgres(_db_config())
            try:
                events = await OutboundRepository(db).fetch_pending(limit=100)
                return [e.id for e in events if e.id in ids]
            finally:
                db.close()

        got = asyncio.run(_run())
        assert got == sorted(ids), f"expected FIFO {sorted(ids)}, got {got}"
    finally:
        _delete_events(conn, ids)
        conn.commit()
        conn.close()


def test_delivered_rows_never_returned():
    """Строка с delivered_at IS NOT NULL не выбирается."""
    tag = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    ids: list[int] = []
    try:
        pending = _insert_event(conn, tag)
        delivered = _insert_event(conn, tag, delivered=True)
        ids = [pending, delivered]
        conn.commit()

        async def _run() -> list[int]:
            db = AsyncPostgres(_db_config())
            try:
                events = await OutboundRepository(db).fetch_pending(limit=100)
                return [e.id for e in events if e.id in ids]
            finally:
                db.close()

        got = asyncio.run(_run())
        assert pending in got
        assert delivered not in got, "доставленная строка не должна выбираться"
    finally:
        _delete_events(conn, ids)
        conn.commit()
        conn.close()


def test_claim_leases_next_attempt_into_future():
    """После claim next_attempt_at заявленной строки сдвинут в будущее, и
    немедленная повторная выборка её НЕ возвращает (lease переживает commit)."""
    tag = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    ids: list[int] = []
    try:
        event_id = _insert_event(conn, tag)
        ids = [event_id]
        conn.commit()

        before = _next_attempt_at(conn, event_id)

        async def _claim_twice() -> tuple[list[int], list[int]]:
            db = AsyncPostgres(_db_config())
            try:
                repo = OutboundRepository(db)
                first = await repo.fetch_pending(limit=100)
                # Сразу повторная выборка: lease уже сдвинул next_attempt_at,
                # строка не готова → не возвращается.
                second = await repo.fetch_pending(limit=100)
                return (
                    [e.id for e in first if e.id in ids],
                    [e.id for e in second if e.id in ids],
                )
            finally:
                db.close()

        first_ids, second_ids = asyncio.run(_claim_twice())
        assert event_id in first_ids, "первая выборка должна заявить строку"
        assert event_id not in second_ids, (
            "lease должен скрыть только что заявленную строку от повторной выборки"
        )

        # Свежее чтение из независимого соединения: next_attempt_at реально
        # сдвинут в будущее (commit заявки виден). rollback сбрасывает snapshot
        # текущей psycopg2-транзакции, чтобы увидеть закоммиченное dispatcher'ом.
        conn.rollback()
        after = _next_attempt_at(conn, event_id)
        assert after > before, (
            f"next_attempt_at должен уехать вперёд: before={before}, after={after}"
        )
        assert after > datetime.now(after.tzinfo), "lease должен быть в будущем"
    finally:
        _delete_events(conn, ids)
        conn.commit()
        conn.close()


# SQL claim'а — точная копия запроса из OutboundRepository.fetch_pending, но
# исполняется напрямую через psycopg2, чтобы две транзакции были РЕАЛЬНО открыты
# одновременно (две сырых connection в отдельных потоках, синхронизированных
# Barrier). Через asyncio.gather на одном event-loop этого добиться нельзя: две
# fetch_pending почти никогда не перекрываются во времени, и тогда непересечение
# обеспечивает один lease (next_attempt_at), даже если SKIP LOCKED убрать —
# тест давал бы иллюзорную защиту от регрессии. Здесь же оба потока обязаны
# войти в свою транзакцию (выполнить UPDATE ... RETURNING) ДО того, как любой из
# них закоммитит: барьер ставится между RETURNING и commit. При живом
# SKIP LOCKED второй поток просто пропускает строки, заблокированные первым, и
# забирает остаток — множества выходят непересекающимися. Если SKIP LOCKED
# убрать, второй поток заблокируется на row-lock первого и провиснет до
# тайм-аута барьера (тест свалится) — ровно та регрессия, которую надо ловить.
_CONCURRENT_CLAIM_SQL = """
    UPDATE outbound_history_events
    SET next_attempt_at = now() + make_interval(secs => 120)
    WHERE id IN (
        SELECT id
        FROM outbound_history_events
        WHERE delivered_at IS NULL
          AND next_attempt_at <= now()
          AND id = ANY(%s)
        ORDER BY id
        LIMIT %s
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id
"""


def test_concurrent_claims_are_disjoint():
    """Два РЕАЛЬНО одновременно открытых claim'а возвращают непересекающиеся
    множества id — это доказывает, что нужен именно `FOR UPDATE SKIP LOCKED`.

    Два сырых psycopg2-соединения в отдельных потоках синхронизированы через
    `threading.Barrier(2)`: каждый поток выполняет claim-`UPDATE ... RETURNING`
    с вложенным `SELECT ... FOR UPDATE SKIP LOCKED`, затем ждёт на барьере и
    только потом коммитит. Значит обе транзакции открыты ОДНОВРЕМЕННО, и
    row-locks первого потока ещё держатся, когда второй делает свой claim.

    Ключевое свойство: тест ОБЯЗАН падать, если из запроса убрать SKIP LOCKED —
    тогда второй поток заблокируется на чужих row-locks и провиснет до тайм-аута
    барьера (поток не дойдёт до wait, барьер сломается по тайм-ауту), либо при
    иной формулировке возникнет пересечение/дедлок. lease (next_attempt_at) тут
    непричастен: к моменту commit'а оба claim'а УЖЕ выполнены, разнести их во
    времени lease не успевает — disjoint-ность держит только SKIP LOCKED.
    """
    tag = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    ids: list[int] = []
    try:
        ids = [_insert_event(conn, tag) for _ in range(_CONCURRENT_ROWS)]
        conn.commit()

        barrier = threading.Barrier(2, timeout=_BARRIER_TIMEOUT_SECONDS)
        results: dict[int, set[int]] = {}
        errors: dict[int, BaseException] = {}
        # Каждый поток забирает половину строк, чтобы при живом SKIP LOCKED обоим
        # хватило работы и претензии пересекались по кандидатам, а не делились
        # тривиально лимитом.
        half = _CONCURRENT_ROWS // 2

        def _worker(slot: int) -> None:
            worker_conn = _sync_conn()
            try:
                with worker_conn.cursor() as cur:
                    cur.execute(_CONCURRENT_CLAIM_SQL, (ids, half))
                    claimed = {int(row[0]) for row in cur.fetchall()}
                # Транзакция ещё открыта, row-locks держатся. Дожидаемся, пока
                # второй поток тоже выполнит свой claim, и лишь затем коммитим.
                barrier.wait()
                worker_conn.commit()
                results[slot] = claimed
            except BaseException as exc:  # пробросим в основной поток
                errors[slot] = exc
                # Разблокируем партнёра, чтобы он не висел на сломанном барьере.
                barrier.abort()
                worker_conn.rollback()
            finally:
                worker_conn.close()

        t0 = threading.Thread(target=_worker, args=(0,))
        t1 = threading.Thread(target=_worker, args=(1,))
        t0.start()
        t1.start()
        t0.join(timeout=_BARRIER_TIMEOUT_SECONDS + 5)
        t1.join(timeout=_BARRIER_TIMEOUT_SECONDS + 5)

        stuck_msg = (
            "потоки claim'а не завершились — вероятно, один заблокировался на "
            "чужом row-lock (SKIP LOCKED убран?)"
        )
        assert not t0.is_alive(), stuck_msg
        assert not t1.is_alive(), stuck_msg
        assert not errors, (
            f"claim в потоке упал (барьер/дедлок? — признак отсутствия "
            f"SKIP LOCKED): {errors}"
        )

        set_a = results[0]
        set_b = results[1]
        overlap = set_a & set_b
        assert not overlap, (
            f"одновременные claim'ы не должны пересекаться, общие id: {overlap}"
        )
        # Оба потока реально что-то забрали (а не один пусто из-за блокировки).
        assert set_a, "первый поток должен заявить строки"
        assert set_b, "второй поток должен заявить строки"
        # Вместе они забрали все 2*half строк без потерь и дублей.
        assert set_a | set_b == set(ids[: 2 * half]), (
            "объединение claim'ов должно покрыть все залоченные строки без "
            f"пересечений: union={sorted(set_a | set_b)}"
        )
    finally:
        _delete_events(conn, ids)
        conn.commit()
        conn.close()


def test_dead_lettered_rows_never_returned():
    """Запаркованная строка (dead_lettered_at IS NOT NULL) не выбирается fetch_pending."""
    tag = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    ids: list[int] = []
    try:
        pending = _insert_event(conn, tag)
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO outbound_history_events
                    (channel, endpoint, payload, dead_lettered_at)
                VALUES (%s, %s, %s::jsonb, now())
                RETURNING id
                """,
                (_TEST_CHANNEL, _TEST_ENDPOINT, f'{{"tag": "{tag}"}}'),
            )
            parked = int(cur.fetchone()[0])
        ids = [pending, parked]
        conn.commit()

        async def _run() -> list[int]:
            db = AsyncPostgres(_db_config())
            try:
                events = await OutboundRepository(db).fetch_pending(limit=100)
                return [e.id for e in events if e.id in ids]
            finally:
                db.close()

        got = asyncio.run(_run())
        assert pending in got
        assert parked not in got, "запаркованная строка не должна выбираться"
    finally:
        _delete_events(conn, ids)
        conn.commit()
        conn.close()


def test_locker_delete_cascades_outbound_events():
    """ON DELETE CASCADE: ФИЗИЧЕСКОЕ удаление локера уносит его outbound-события.

    Это автоматизирует ровно ту admin-очистку, что раньше оставляла мусорные
    события в очереди (hard-delete локера → его недоставленные строки исчезают).
    Soft-delete (state='archived') строку локера не трогает — здесь именно DELETE.
    """
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    locker_id = None
    event_id: int | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO lockers (inventory_number) VALUES (%s) RETURNING id",
                (f"OUTBOX-FK-{suffix}",),
            )
            locker_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO outbound_history_events
                    (channel, endpoint, payload, locker_id)
                VALUES (%s, %s, '{}'::jsonb, %s)
                RETURNING id
                """,
                (_TEST_CHANNEL, _TEST_ENDPOINT, locker_id),
            )
            event_id = int(cur.fetchone()[0])
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
        conn.commit()
        locker_id = None  # уже удалён — finally его не трогает

        with conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM outbound_history_events WHERE id = %s",
                (event_id,),
            )
            remaining = int(cur.fetchone()[0])
        assert remaining == 0, "событие должно каскадно удалиться вместе с локером"
        event_id = None  # каскад уже удалил
    finally:
        if event_id is not None:
            _delete_events(conn, [event_id])
        if locker_id is not None:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))
        conn.commit()
        conn.close()


def test_byte_budget_sql_runs_and_does_not_falsely_drop():
    """Бюджет по байтам — валидный SQL на реальном PG (window sum + COALESCE):
    при щедром max_bytes все готовые строки возвращаются (без ложного отсечения).

    Отсечение префикса по бюджету проверяется на уровне формы SQL в unit-тестах;
    здесь важно, что CTE с pg_column_size/row_number действительно исполняется
    Postgres'ом и не теряет строки, когда бюджет заведомо велик."""
    tag = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    ids: list[int] = []
    try:
        big = "x" * 4096
        with conn.cursor() as cur:
            for _ in range(4):
                cur.execute(
                    """
                    INSERT INTO outbound_history_events (channel, endpoint, payload)
                    VALUES (%s, %s, %s::jsonb)
                    RETURNING id
                    """,
                    (_TEST_CHANNEL, _TEST_ENDPOINT,
                     f'{{"tag": "{tag}", "blob": "{big}"}}'),
                )
                ids.append(int(cur.fetchone()[0]))
        conn.commit()

        async def _run() -> list[int]:
            db = AsyncPostgres(_db_config())
            try:
                events = await OutboundRepository(db).fetch_pending(
                    limit=100, max_bytes=10_000_000,
                )
                return [e.id for e in events if e.id in ids]
            finally:
                db.close()

        got = asyncio.run(_run())
        assert set(got) == set(ids), f"щедрый бюджет не должен терять строки: {got}"
    finally:
        _delete_events(conn, ids)
        conn.commit()
        conn.close()
