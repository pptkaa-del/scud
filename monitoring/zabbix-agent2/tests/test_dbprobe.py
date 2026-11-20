"""Offline unit tests for the batched dbprobe.sh.

A fake ``psql`` on PATH answers SHOW POOLS / SHOW STATS / SELECT and writes a
benign warning to stderr. The tests assert that:
  * the COMBINED output (what zabbix-agent2 ingests) is a clean scalar, never the
    stderr warning;
  * the 'max' aggregation prints 0 when every value is 0 (quiet pool);
  * a real psql failure yields a non-zero exit and no value (NOTSUPPORTED);
  * the per-host cache means a second metric read does NOT reconnect.

No live stack required. Each test uses an isolated cache dir so they don't share
state. Run via the monitoring test image (python:3.13-slim has sh + awk + flock).
"""
import os
import stat
import subprocess
import textwrap

HERE = os.path.dirname(__file__)
DBPROBE = os.path.abspath(os.path.join(HERE, "..", "dbprobe.sh"))
SECRETS_DIR = "/run/secrets"

_SHIM = """\
#!/bin/sh
echo x >> "$DBPROBE_TEST_COUNTER"
mode=
for a in "$@"; do
    case "$a" in
        *"SHOW POOLS"*) mode=pools ;;
        *"SHOW STATS"*) mode=stats ;;
        *SELECT*)       mode=pg ;;
    esac
done
printf 'WARNING: benign notice that must not reach the value\\n' >&2
case "$mode" in
    pools)
        printf 'database|user|cl_waiting|sv_active|maxwait|maxwait_us|pool_mode\\n'
        printf 'scud|acm_ro|2|3|0|0|transaction\\n'
        printf 'scud|admin|1|1|0|0|transaction\\n'
        printf '(2 rows)\\n' ;;
    stats)
        printf 'database|total_query_time|avg_query_time\\n'
        printf 'scud|0|0\\n'
        printf '(1 row)\\n' ;;
    pg)
        # numbackends|max_conn|deadlocks|xact_rollback|longest_txn|hist_bytes|
        # outbox_pending|outbox_oldest|outbox_bytes|outbox_retrying|outbox_dl|
        # history_orphan_media
        printf '5|100|0|0|0|0|42|99|123456|3|1|7\\n' ;;
esac
"""

_SHIM_FAIL = """\
#!/bin/sh
echo x >> "$DBPROBE_TEST_COUNTER"
printf 'FATAL: connection refused\\n' >&2
exit 2
"""


def _write_secret(name):
    os.makedirs(SECRETS_DIR, exist_ok=True)
    with open(os.path.join(SECRETS_DIR, name), "w", encoding="utf-8") as fh:
        fh.write("dummy-password\n")


def _make_shim(tmp_path, body):
    shim = tmp_path / "psql"
    shim.write_text(body)
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _run(tmp_path, args, body=_SHIM):
    _make_shim(tmp_path, body)
    counter = tmp_path / "calls"
    counter.write_text("")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["DBPROBE_CACHE_DIR"] = str(tmp_path / "cache")
    env["DBPROBE_CACHE_TTL"] = "25"
    env["DBPROBE_TEST_COUNTER"] = str(counter)
    # Capture stdout AND stderr together — exactly what zabbix-agent2 ingests.
    res = subprocess.run(
        ["sh", DBPROBE, *args], env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, timeout=30, check=False,
    )
    res.psql_calls = len(counter.read_text().split()) if counter.exists() else 0
    return res, env


def test_pg_scalar_clean_value(tmp_path):
    _write_secret("postgres_user_password")
    res, _ = _run(tmp_path, ["pg", "postgres_user", "numbackends"])
    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == "5"
    assert "WARNING" not in res.stdout


def test_pgb_sum_clean_value(tmp_path):
    _write_secret("pgb_admin_password")
    res, _ = _run(tmp_path, ["pgb", "pgbouncer_user", "cl_waiting"])
    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == "3"           # 2 + 1 summed
    assert "WARNING" not in res.stdout


def test_pgb_max_all_zero_emits_zero(tmp_path):
    _write_secret("pgb_admin_password")
    res, _ = _run(tmp_path, ["pgb", "pgbouncer_user", "maxwait"])
    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == "0"           # all-zero max must be 0, not ""


def test_real_failure_is_notsupported(tmp_path):
    _write_secret("postgres_user_password")
    res, _ = _run(tmp_path, ["pg", "postgres_user", "numbackends"], body=_SHIM_FAIL)
    assert res.returncode != 0
    assert res.stdout.strip() == ""


def test_history_partition_bytes_rejected_off_history(tmp_path):
    _write_secret("postgres_user_password")
    res, _ = _run(tmp_path, ["pg", "postgres_user", "history_partition_bytes"])
    assert res.returncode != 0
    assert res.stdout.strip() == ""


def test_outbox_pending_on_locker(tmp_path):
    """outbox_* scalars are served for postgres_locker (column f7 = outbox_pending)."""
    _write_secret("postgres_locker_password")
    res, _ = _run(tmp_path, ["pg", "postgres_locker", "outbox_pending"])
    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == "42"
    assert "WARNING" not in res.stdout


def test_outbox_rejected_off_locker(tmp_path):
    """outbox_* exist only on the locker instance; other instances → NOTSUPPORTED."""
    _write_secret("postgres_user_password")
    res, _ = _run(tmp_path, ["pg", "postgres_user", "outbox_pending"])
    assert res.returncode != 0
    assert res.stdout.strip() == ""


def test_cache_avoids_reconnect_on_second_read(tmp_path):
    """First metric read builds the cache (1 psql SELECT); a second read of a
    different metric for the same host must hit the cache (no extra psql)."""
    _write_secret("postgres_user_password")
    _make_shim(tmp_path, _SHIM)
    counter = tmp_path / "calls"
    counter.write_text("")
    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"
    env["DBPROBE_CACHE_DIR"] = str(tmp_path / "cache")
    env["DBPROBE_CACHE_TTL"] = "25"
    env["DBPROBE_TEST_COUNTER"] = str(counter)
    for metric in ("numbackends", "deadlocks"):
        r = subprocess.run(["sh", DBPROBE, "pg", "postgres_user", metric], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                           timeout=30, check=False)
        assert r.returncode == 0, r.stdout
    assert len(counter.read_text().split()) == 1, "second read should hit the cache, not reconnect"


def test_history_orphan_media_on_history(tmp_path):
    """history_orphan_media (column f12) is served for postgres_history."""
    _write_secret("postgres_history_password")
    res, _ = _run(tmp_path, ["pg", "postgres_history", "history_orphan_media"])
    assert res.returncode == 0, res.stdout
    assert res.stdout.strip() == "7"


def test_history_orphan_media_rejected_off_history(tmp_path):
    _write_secret("postgres_locker_password")
    res, _ = _run(tmp_path, ["pg", "postgres_locker", "history_orphan_media"])
    assert res.returncode != 0
    assert res.stdout.strip() == ""


_SHIM_RECORD_SQL = _SHIM.replace(
    'echo x >> "$DBPROBE_TEST_COUNTER"',
    'echo x >> "$DBPROBE_TEST_COUNTER"\nprintf \'%s\n\' "$@" > "$DBPROBE_TEST_COUNTER.sql"',
)


def test_history_orphan_media_guarded_by_column_probe(tmp_path):
    """До DDL §6 колонки security_event_uid нет: подзапрос обязан проверять её
    наличие и не ссылаться на неё напрямую, иначе падает вся строка history."""
    _write_secret("postgres_history_password")
    res, _ = _run(tmp_path, ["pg", "postgres_history", "numbackends"], body=_SHIM_RECORD_SQL)
    assert res.returncode == 0, res.stdout
    sql = (tmp_path / "calls.sql").read_text()
    assert "information_schema.columns" in sql
    assert "column_name = 'security_event_uid'" in sql
    assert "query_to_xml(" in sql
    assert "WHERE security_event_id IS NULL AND security_event_uid IS NOT NULL AND deleted_at IS NULL AND uploaded_at < now() - interval '10 minutes')" not in sql
