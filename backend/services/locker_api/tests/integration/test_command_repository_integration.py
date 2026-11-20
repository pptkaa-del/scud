"""Real-DB integration тесты CommandRepository — cross-locker spoofing.

Покрывают то, что unit-тесты с моком db.fetch_one не могут: что SQL действительно
содержит фильтр AND locker_id = %s и Postgres его enforce'ит. Если кто-то
удалит этот фильтр в репозитории, unit-тесты с моком останутся зелёными
(mock возвращает None независимо от SQL), а вот этот integration тест поймает.
"""
from __future__ import annotations

import asyncio
import os
import uuid

from dacite import from_dict
import psycopg2

from app.locker_data.db.models.command import CommandStatus, CommandType, LockerCommand
from app.locker_data.db.repositories.command import _DACITE_CONFIG, CommandRepository
from app.services.command_expiry import COMMAND_TTL_FAILURE_REASON
from app.services.history_producers.command_history_producer import CommandHistoryProducer
from shared.database import AsyncPostgres, PostgresConfig


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


def _insert_locker(conn, inv_suffix: str) -> str:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lockers (inventory_number, state)
            VALUES (%s, 'active')
            RETURNING id
            """,
            (f"CMD-INT-{inv_suffix}",),
        )
        return str(cur.fetchone()[0])


def _insert_command(conn, locker_id: str, command_type: str = "open") -> str:
    """Создать pending-команду в БД, минуя HTTP API."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO locker_commands (locker_id, type, status, created_by, parameters)
            VALUES (%s, %s, 'pending', %s, '{}'::jsonb)
            RETURNING id
            """,
            (locker_id, command_type, 99),
        )
        return str(cur.fetchone()[0])


def _insert_sent_command(
    conn,
    locker_id: str,
    *,
    retry_count: int,
    max_retries: int,
    stale_minutes: int,
    command_type: str = "open",
) -> str:
    """Создать команду в статусе 'sent' с заданным retry_count/max_retries и
    sent_at в прошлом (для ack-timeout sweep тестов)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO locker_commands
                (locker_id, type, status, created_by, parameters,
                 sent_at, retry_count, max_retries)
            VALUES (%s, %s, 'sent', %s, '{}'::jsonb,
                    NOW() - make_interval(mins => %s), %s, %s)
            RETURNING id
            """,
            (locker_id, command_type, 99, stale_minutes, retry_count, max_retries),
        )
        return str(cur.fetchone()[0])


def _fetch_command(conn, command_id: str) -> LockerCommand:
    """Прочитать команду напрямую (минуя репозиторий, чтобы не зависеть от его кода)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id::text, locker_id::text, type, status, parameters,
                   created_at, created_by, sent_at, delivered_at, executed_at,
                   failed_at, result, error_message, retry_count, max_retries
            FROM locker_commands
            WHERE id = %s
            """,
            (command_id,),
        )
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        return from_dict(LockerCommand, dict(zip(cols, row, strict=False)), config=_DACITE_CONFIG)


def _delete_locker(conn, locker_id: str) -> None:
    """Hard-delete локера (cascade удалит и команды)."""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM lockers WHERE id = %s", (locker_id,))


async def _spoofing_executed_async(locker_a: str, locker_b: str, cmd_b: str) -> None:
    """Устройство A пытается обновить статус команды локера B. Должно вернуть None
    и не тронуть БД-строку."""
    config = _db_config()
    db = AsyncPostgres(config)
    try:
        repo = CommandRepository(db)
        result = await repo.update_status_to_executed(
            cmd_b, locker_a, {"spoofed": "by_A"}
        )
        assert result is None, f"Expected None for cross-locker, got {result}"
    finally:
        db.close()


async def _spoofing_failed_async(locker_a: str, cmd_b: str) -> None:
    config = _db_config()
    db = AsyncPostgres(config)
    try:
        repo = CommandRepository(db)
        result = await repo.update_status_to_failed(cmd_b, locker_a, "spoof attempt")
        assert result is None, f"Expected None for cross-locker, got {result}"
    finally:
        db.close()


async def _legit_executed_async(locker_b: str, cmd_b: str) -> LockerCommand:
    """Easy-path: тот же локер ставит executed — должно сработать."""
    config = _db_config()
    db = AsyncPostgres(config)
    try:
        repo = CommandRepository(db)
        result = await repo.update_status_to_executed(cmd_b, locker_b, {"ok": True})
        assert result is not None
        return result
    finally:
        db.close()


def test_update_status_to_executed_cross_locker_blocked():
    """Real-DB cross-locker spoofing на executed-пути."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker_a = _insert_locker(conn, f"A-{suffix}")
        locker_b = _insert_locker(conn, f"B-{suffix}")
        conn.commit()
        cmd_b = _insert_command(conn, locker_b)
        conn.commit()

        # Spoofing попытка: устройство A с UUID команды локера B
        asyncio.run(_spoofing_executed_async(locker_a, locker_b, cmd_b))

        # Команда в БД должна быть нетронутой
        fresh = _fetch_command(conn, cmd_b)
        assert fresh.status == CommandStatus.PENDING
        assert fresh.executed_at is None
        assert fresh.result == {}

        # Sanity check: тот же UUID, но правильный locker_id — UPDATE проходит
        legit = asyncio.run(_legit_executed_async(locker_b, cmd_b))
        assert legit.status == CommandStatus.EXECUTED
        assert legit.result == {"ok": True}
    finally:
        _delete_locker(conn, locker_a)
        _delete_locker(conn, locker_b)
        conn.commit()
        conn.close()


def test_update_status_to_failed_cross_locker_blocked():
    """Real-DB cross-locker spoofing на failed-пути (это та самая дырка
    в моём первом фиксе — get_by_id без locker_id-фильтра приводил к
    публикации failed по чужой команде)."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker_a = _insert_locker(conn, f"A-{suffix}")
        locker_b = _insert_locker(conn, f"B-{suffix}")
        conn.commit()
        cmd_b = _insert_command(conn, locker_b, command_type="reboot")
        conn.commit()

        asyncio.run(_spoofing_failed_async(locker_a, cmd_b))

        fresh = _fetch_command(conn, cmd_b)
        assert fresh.status == CommandStatus.PENDING
        assert fresh.failed_at is None
        assert fresh.error_message is None
    finally:
        _delete_locker(conn, locker_a)
        _delete_locker(conn, locker_b)
        conn.commit()
        conn.close()


def test_ack_timeout_sweep_splits_pending_and_failed():
    """COR-15 real-DB: просроченный 'sent' с исчерпанным бюджетом
    (retry_count+1 >= max_retries) → терминальный failed; с оставшимся
    бюджетом → pending (retry_count+1). Не-просроченный 'sent' не трогается."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker = _insert_locker(conn, f"SWEEP-{suffix}")
        conn.commit()
        # Исчерпанная: retry_count=2, max_retries=3 → 2+1>=3 → failed.
        exhausted = _insert_sent_command(
            conn, locker, retry_count=2, max_retries=3, stale_minutes=10)
        # С остатком: retry_count=0, max_retries=3 → 0+1<3 → pending.
        retryable = _insert_sent_command(
            conn, locker, retry_count=0, max_retries=3, stale_minutes=10)
        # Свежая (не просрочена) — sweep её не трогает.
        fresh_sent = _insert_sent_command(
            conn, locker, retry_count=0, max_retries=3, stale_minutes=0)
        conn.commit()

        async def _run() -> list[LockerCommand]:
            config = _db_config()
            db = AsyncPostgres(config)
            try:
                repo = CommandRepository(db)
                reset = await repo.reset_timed_out_sent_commands(5)
                assert len(reset) == 1, f"expected 1 reset, got {len(reset)}"
                return await repo.fail_exhausted_sent_commands(5, "ack timeout")
            finally:
                db.close()

        failed_rows = asyncio.run(_run())
        failed_ids = {c.id for c in failed_rows}
        assert exhausted in failed_ids, "exhausted команда должна попасть в failed"

        ex_after = _fetch_command(conn, exhausted)
        assert ex_after.status == CommandStatus.FAILED
        assert ex_after.failed_at is not None
        assert ex_after.error_message == "ack timeout"

        rt_after = _fetch_command(conn, retryable)
        assert rt_after.status == CommandStatus.PENDING
        assert rt_after.retry_count == 1  # +1 на reset

        fresh_after = _fetch_command(conn, fresh_sent)
        assert fresh_after.status == CommandStatus.SENT, (
            "не просроченная sent-команда не должна трогаться sweep'ом"
        )
    finally:
        _delete_locker(conn, locker)
        conn.commit()
        conn.close()


def test_update_status_to_sent_claim_single_winner_under_concurrency():
    """COR-02 real-DB: два параллельных update_status_to_sent на ОДНУ pending
    команду → ровно один claim выигрывает (получает строку), второй — None.

    Это и есть гонка inline-доставки против queue-processor: оба зовут
    update_status_to_sent для одной команды. CAS `WHERE status='pending'`
    сериализуется Postgres'ом, поэтому RETURNING непуст ровно у одного.
    Unit-тест с моком db.fetch_one этого доказать не может (mock не знает про
    конкуренцию); только реальный UPDATE ... WHERE ... RETURNING ловит регрессию,
    если кто-то вернёт безусловный апдейт.
    """
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker = _insert_locker(conn, f"CLAIM-{suffix}")
        conn.commit()
        cmd = _insert_command(conn, locker)
        conn.commit()

        async def _run() -> list[LockerCommand | None]:
            config = _db_config()

            async def _claim() -> LockerCommand | None:
                db = AsyncPostgres(config)
                try:
                    return await CommandRepository(db).update_status_to_sent(cmd, ttl_s=60)
                finally:
                    db.close()

            return await asyncio.gather(_claim(), _claim())

        results = asyncio.run(_run())
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, (
            f"expected exactly one successful claim, got {results}"
        )
        assert winners[0].status == CommandStatus.SENT

        # В БД команда ровно один раз перешла в sent.
        fresh = _fetch_command(conn, cmd)
        assert fresh.status == CommandStatus.SENT
        assert fresh.sent_at is not None
    finally:
        _delete_locker(conn, locker)
        conn.commit()
        conn.close()


def test_update_status_to_sent_does_not_clobber_executed():
    """COR-02 real-DB: запоздавший inline update_status_to_sent НЕ откатывает
    уже executed-команду обратно в sent (CAS-guard status='pending')."""
    suffix = uuid.uuid4().hex[:8]
    conn = _sync_conn()
    try:
        locker = _insert_locker(conn, f"CLOBBER-{suffix}")
        conn.commit()
        cmd = _insert_command(conn, locker)
        conn.commit()

        async def _run() -> LockerCommand | None:
            config = _db_config()
            db = AsyncPostgres(config)
            try:
                repo = CommandRepository(db)
                # Устройство уже ACK'нуло команду как executed.
                await repo.update_status_to_executed(cmd, locker, {"ok": True})
                # Запоздавший inline-апдейт пытается пометить sent — должен
                # вернуть None (строки в pending нет) и не тронуть статус.
                return await repo.update_status_to_sent(cmd, ttl_s=60)
            finally:
                db.close()

        late = asyncio.run(_run())
        assert late is None, "late sent-claim must not find a pending row"

        fresh = _fetch_command(conn, cmd)
        assert fresh.status == CommandStatus.EXECUTED, (
            "executed команда не должна откатиться в sent"
        )
    finally:
        _delete_locker(conn, locker)
        conn.commit()
        conn.close()


# Живой locker_api в стеке сам выметает команды старше 60 с, поэтому «старые»
# строки здесь — 30 с при ttl_s=10, «свежие» — now(): сервис не трогает ни те, ни другие.
_OLD_AGE_S = 30
_TEST_TTL_S = 10


def _insert_command_aged(conn, locker_id: str, status: str, age_s: int, *,
                         sent: bool = False, command_type: str = "open") -> str:
    executed = status == CommandStatus.EXECUTED.value
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO locker_commands
                (locker_id, type, status, created_by, parameters, created_at,
                 sent_at, executed_at, result)
            VALUES (%s, %s, %s::command_status, 99, '{"relay": 1}'::jsonb,
                    now() - make_interval(secs => %s),
                    CASE WHEN %s THEN now() - make_interval(secs => %s) END,
                    CASE WHEN %s THEN now() - make_interval(secs => %s) END,
                    CASE WHEN %s THEN '{"ok": true}'::jsonb ELSE '{}'::jsonb END)
            RETURNING id
            """,
            (locker_id, command_type, status, age_s, sent, age_s, executed, age_s, executed),
        )
        return str(cur.fetchone()[0])


def _outbox_rows(conn, locker_id: str, channel: str) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM outbound_history_events "
            "WHERE locker_id = %s AND channel = %s ORDER BY id",
            (locker_id, channel),
        )
        return [r[0] for r in cur.fetchall()]


def _with_repo(fn):
    async def _run():
        db = AsyncPostgres(_db_config())
        try:
            return await fn(CommandRepository(db))
        finally:
            db.close()
    return asyncio.run(_run())


def _expire(repo: CommandRepository, locker: str):
    return repo.expire_stale_commands(
        ttl_s=_TEST_TTL_S,
        error_message=COMMAND_TTL_FAILURE_REASON,
        outbox_channel=CommandHistoryProducer.CHANNEL,
        outbox_endpoint=CommandHistoryProducer.ENDPOINT,
        locker_id=locker,
    )


def _autocommit_conn():
    conn = _sync_conn()
    conn.autocommit = True
    return conn


def test_expire_stale_commands_fails_old_active_and_enqueues_history_atomically():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTL-{uuid.uuid4().hex[:8]}")
    try:
        old_pending = _insert_command_aged(conn, locker, "pending", _OLD_AGE_S)
        old_sent = _insert_command_aged(conn, locker, "sent", _OLD_AGE_S, sent=True)
        old_delivered = _insert_command_aged(conn, locker, "delivered", _OLD_AGE_S, sent=True)
        old_executed = _insert_command_aged(conn, locker, "executed", _OLD_AGE_S)
        fresh_pending = _insert_command_aged(conn, locker, "pending", 0)
        executed_before = _fetch_command(conn, old_executed)

        expired = _with_repo(lambda repo: _expire(repo, locker))

        expired_ids = {c.id for c in expired}
        assert expired_ids == {old_pending, old_sent, old_delivered}
        for cid in expired_ids:
            row = _fetch_command(conn, cid)
            assert row.status == CommandStatus.FAILED
            assert row.failed_at is not None
            assert row.error_message == COMMAND_TTL_FAILURE_REASON
            assert row.result == {"success": False, "message": COMMAND_TTL_FAILURE_REASON}
        assert _fetch_command(conn, fresh_pending).status == CommandStatus.PENDING
        executed_after = _fetch_command(conn, old_executed)
        assert executed_after.status == CommandStatus.EXECUTED
        assert executed_after.executed_at == executed_before.executed_at
        assert executed_after.result == {"ok": True}

        outbox = _outbox_rows(conn, locker, CommandHistoryProducer.CHANNEL)
        assert sorted(p["command_id"] for p in outbox) == sorted(expired_ids)
        for payload in outbox:
            assert payload == CommandHistoryProducer._payload(
                command_id=payload["command_id"],
                locker_id=locker,
                command_type="open",
                status="failed",
                parameters={"relay": 1},
                created_by=99,
                error_message=COMMAND_TTL_FAILURE_REASON,
            )
    finally:
        _delete_locker(conn, locker)
        conn.close()


def test_get_pending_commands_excludes_expired():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTLP-{uuid.uuid4().hex[:8]}")
    try:
        _insert_command_aged(conn, locker, "pending", _OLD_AGE_S)
        fresh = _insert_command_aged(conn, locker, "pending", 0)
        rows = _with_repo(lambda repo: repo.get_pending_commands(
            ttl_s=_TEST_TTL_S, locker_id=locker))
        assert [c.id for c in rows] == [fresh]
    finally:
        _delete_locker(conn, locker)
        conn.close()


def test_claim_refuses_expired_pending():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTLC-{uuid.uuid4().hex[:8]}")
    try:
        old = _insert_command_aged(conn, locker, "pending", _OLD_AGE_S)
        fresh = _insert_command_aged(conn, locker, "pending", 0)
        assert _with_repo(lambda repo: repo.update_status_to_sent(old, ttl_s=_TEST_TTL_S)) is None
        assert _fetch_command(conn, old).status == CommandStatus.PENDING
        claimed = _with_repo(lambda repo: repo.update_status_to_sent(fresh, ttl_s=_TEST_TTL_S))
        assert claimed is not None
        assert claimed.status == CommandStatus.SENT
    finally:
        _delete_locker(conn, locker)
        conn.close()


def test_executed_does_not_overwrite_terminal():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTLE-{uuid.uuid4().hex[:8]}")
    try:
        cmd = _insert_command_aged(conn, locker, "sent", 0, sent=True)
        first = _with_repo(lambda repo: repo.update_status_to_executed(cmd, locker, {"n": 1}))
        assert first is not None
        before = _fetch_command(conn, cmd)
        second = _with_repo(lambda repo: repo.update_status_to_executed(cmd, locker, {"n": 2}))
        assert second is None
        after = _fetch_command(conn, cmd)
        assert after.executed_at == before.executed_at
        assert after.result == {"n": 1}
    finally:
        _delete_locker(conn, locker)
        conn.close()


def test_late_success_does_not_flip_failed():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTLF-{uuid.uuid4().hex[:8]}")
    try:
        cmd = _insert_command_aged(conn, locker, "sent", 0, sent=True)
        assert _with_repo(lambda repo: repo.update_status_to_failed(
            cmd, locker, COMMAND_TTL_FAILURE_REASON)) is not None
        before = _fetch_command(conn, cmd)
        assert _with_repo(lambda repo: repo.update_status_to_executed(
            cmd, locker, {"ok": True})) is None
        after = _fetch_command(conn, cmd)
        assert after.status == CommandStatus.FAILED
        assert after.executed_at is None
        assert after.failed_at == before.failed_at
        assert after.result == before.result
        assert after.error_message == COMMAND_TTL_FAILURE_REASON
    finally:
        _delete_locker(conn, locker)
        conn.close()


def test_late_failure_does_not_flip_executed():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTLX-{uuid.uuid4().hex[:8]}")
    try:
        cmd = _insert_command_aged(conn, locker, "sent", 0, sent=True)
        assert _with_repo(lambda repo: repo.update_status_to_executed(
            cmd, locker, {"ok": True})) is not None
        assert _with_repo(lambda repo: repo.update_status_to_failed(
            cmd, locker, "failed", result={"success": False})) is None
        after = _fetch_command(conn, cmd)
        assert after.status == CommandStatus.EXECUTED
        assert after.failed_at is None
        assert after.error_message is None
        assert after.result == {"ok": True}
    finally:
        _delete_locker(conn, locker)
        conn.close()


def test_release_claim_only_from_sent():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTLR-{uuid.uuid4().hex[:8]}")
    try:
        sent = _insert_command_aged(conn, locker, "sent", 0, sent=True)
        executed = _insert_command_aged(conn, locker, "executed", 0)
        _with_repo(lambda repo: repo.release_claim(sent))
        _with_repo(lambda repo: repo.release_claim(executed))
        released = _fetch_command(conn, sent)
        assert released.status == CommandStatus.PENDING
        assert released.sent_at is None
        assert _fetch_command(conn, executed).status == CommandStatus.EXECUTED
    finally:
        _delete_locker(conn, locker)
        conn.close()


def _outbox_terminal_count(conn, command_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM outbound_history_events "
            "WHERE channel = %s AND payload->>'command_id' = %s "
            "AND payload->>'status' IN ('executed', 'failed')",
            (CommandHistoryProducer.CHANNEL, command_id),
        )
        return int(cur.fetchone()[0])


def test_sweep_vs_rep_cas_race_single_terminal():
    conn = _autocommit_conn()
    locker = _insert_locker(conn, f"TTLRACE-{uuid.uuid4().hex[:8]}")
    config = _db_config()
    try:
        for i in range(20):
            cmd = _insert_command_aged(conn, locker, "sent", _OLD_AGE_S, sent=True)

            async def _race(cmd: str = cmd, sweep_first: bool = i % 2 == 0):
                db_sweep, db_rep = AsyncPostgres(config), AsyncPostgres(config)
                try:
                    sweep = _expire(CommandRepository(db_sweep), locker)
                    rep = CommandRepository(db_rep).update_status_to_executed(
                        cmd, locker, {"ok": True})
                    if sweep_first:
                        swept, executed = await asyncio.gather(sweep, rep)
                    else:
                        executed, swept = await asyncio.gather(rep, sweep)
                    return swept, executed
                finally:
                    db_sweep.close()
                    db_rep.close()

            swept, executed = asyncio.run(_race())
            sweep_won = any(c.id == cmd for c in swept)
            assert sweep_won != (executed is not None), (i, swept, executed)
            final = _fetch_command(conn, cmd)
            if sweep_won:
                assert final.status == CommandStatus.FAILED
                assert _outbox_terminal_count(conn, cmd) == 1
            else:
                assert final.status == CommandStatus.EXECUTED
                assert _outbox_terminal_count(conn, cmd) == 0
    finally:
        _delete_locker(conn, locker)
        conn.close()
