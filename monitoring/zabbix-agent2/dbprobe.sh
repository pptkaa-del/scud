#!/bin/sh
# dbprobe.sh — batched PgBouncer / PostgreSQL probe for zabbix-agent2.
#
# Usage: dbprobe.sh <source> <host> <metric>
#   source : pgb | pg
#   host   : full docker hostname — pgbouncer_<svc> (pgb) | postgres_<svc> (pg)
#   metric : one of the fixed tokens handled below.
#
# Batching: a single (source,host) opens ONE cold psql connection per cache TTL
# and fetches ALL of that host's metrics at once into a short-lived cache; every
# scud.dbprobe[<source>,<host>,<metric>] item then serves from that cache. This
# collapses the ~52 passive probes from one cold connection per item to ~one per
# instance per cycle, so a quiet stand no longer times out under connection-pool
# contention. A flock serialises the rebuild so concurrent items don't stampede.
#
# Contract (R4): prints EXACTLY ONE scalar number to stdout on success; on ANY
# error prints nothing and exits non-zero so zabbix-agent2 marks the item
# NOTSUPPORTED. Credentials are read from mounted Docker secret files via
# PGPASSWORD and are NEVER printed.
set -eu

PGB_PORT=6432
PGB_USER=pgb_admin
PGB_DB=pgbouncer
PGB_SECRET=/run/secrets/pgb_admin_password
PG_PORT=5432
# Superuser role of the postgres instances (POSTGRES_USER=admin in compose).
PG_USER='admin'

# Cache: TTL must be below the item delay (30s) so each cycle refreshes once.
CACHE_DIR="${DBPROBE_CACHE_DIR:-/tmp/scud-dbprobe}"
CACHE_TTL="${DBPROBE_CACHE_TTL:-25}"

SOURCE="${1:-}"
HOST="${2:-}"
METRIC="${3:-}"
[ -n "$SOURCE" ] && [ -n "$HOST" ] && [ -n "$METRIC" ] || exit 1

# read_secret <path> — prints file contents without trailing CR/LF; non-zero
# (and no output) if the file is missing or empty.
read_secret() {
    [ -s "$1" ] || return 1
    tr -d '\r\n' < "$1"
}

# emit_int <value> — print one integer, or exit non-zero with no stdout if the
# value is empty / not a non-negative integer. Single source of the "one scalar
# or nothing" guarantee.
emit_int() {
    case "$1" in
        ''|*[!0-9]*) exit 1 ;;
        *) printf '%s\n' "$1" ;;
    esac
}

# postgres_<svc> -> scud_<db> suffix. Mirrors POSTGRES_DB in docker-compose.yaml.
pg_dbsuffix() {
    case "$HOST" in
        postgres_user)         printf 'users' ;;
        postgres_locker)       printf 'lockers' ;;
        postgres_history)      printf 'history' ;;
        postgres_auth)         printf 'auth' ;;
        postgres_notification) printf 'notifications' ;;
        postgres_reports)      printf 'reports' ;;
        *) return 1 ;;
    esac
}

# ---- PgBouncer admin console aggregation (multi-row text) -------------------
# Keep the header row (psql -A, NO -t) so awk locates the target column by NAME.
# m/s are initialised to 0 so an all-zero 'max' (e.g. maxwait on a quiet pool)
# prints 0 rather than an empty string.
pgb_agg() {  # <agg> <column>  ; table text on stdin -> one integer on stdout
    awk -F'|' -v agg="$1" -v col="$2" '
        BEGIN { m = 0; s = 0 }
        NR == 1 { for (i = 1; i <= NF; i++) if ($i == col) c = i; next }
        /^\([0-9]+ row/ { next }       # skip psql "(N rows)" footer
        c == 0 { next }
        $c ~ /^[0-9]+$/ {
            if (agg == "max") { if ($c > m) m = $c }
            else              { s += $c }
            seen = 1
        }
        END {
            if (!seen) { print 0; exit }
            if (agg == "max") print m; else print s
        }'
}

# ---- cache build: emit "metric value" lines for every metric of this host ---
build_pgb() {  # ONE connection: SHOW POOLS + SHOW STATS
    PGPASSWORD="$(read_secret "$PGB_SECRET")" || return 1
    export PGPASSWORD
    _pools="$(psql -h "$HOST" -p "$PGB_PORT" -U "$PGB_USER" -d "$PGB_DB" \
                  -A --no-psqlrc -v ON_ERROR_STOP=1 -c 'SHOW POOLS;' 2>/dev/null)" || return 1
    _stats="$(psql -h "$HOST" -p "$PGB_PORT" -U "$PGB_USER" -d "$PGB_DB" \
                  -A --no-psqlrc -v ON_ERROR_STOP=1 -c 'SHOW STATS;' 2>/dev/null)" || return 1
    printf 'cl_waiting %s\n'     "$(printf '%s\n' "$_pools" | pgb_agg sum cl_waiting)"
    printf 'sv_active %s\n'      "$(printf '%s\n' "$_pools" | pgb_agg sum sv_active)"
    printf 'maxwait %s\n'        "$(printf '%s\n' "$_pools" | pgb_agg max maxwait)"
    printf 'avg_query_time %s\n' "$(printf '%s\n' "$_stats" | pgb_agg sum avg_query_time)"
}

build_pg() {  # ONE connection: all pg_stat_* scalars in a single row
    _secret="/run/secrets/${HOST}_password"
    PGPASSWORD="$(read_secret "$_secret")" || return 1
    export PGPASSWORD
    _dbname="scud_$(pg_dbsuffix)" || return 1
    # history_partition_bytes is meaningful only on the history DB; 0 elsewhere.
    _hpb='0'; _orphan='0'
    if [ "$HOST" = postgres_history ]; then
        _hpb="COALESCE((SELECT sum(pg_total_relation_size(inhrelid)) FROM pg_inherits JOIN pg_class p ON p.oid = pg_inherits.inhparent WHERE p.relname IN ('telemetry', 'security_events')), 0)::bigint"
        # Relay-фото кадры, чьё intrusion-событие так и не доехало. Колонка
        # security_event_uid появляется только после DDL (DEPLOYMENT.md §6):
        # до неё подзапрос через query_to_xml не парсится вовсе и даёт 0,
        # а не роняет всю строку history-метрик.
        _orphan="CASE WHEN EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'locker_media' AND column_name = 'security_event_uid') THEN (xpath('/row/c/text()', query_to_xml('SELECT count(*) AS c FROM locker_media WHERE security_event_id IS NULL AND security_event_uid IS NOT NULL AND deleted_at IS NULL AND uploaded_at < now() - interval ''10 minutes''', false, true, '')))[1]::text::bigint ELSE 0 END"
    fi
    # Outbox (outbound_history_events → history_api) живёт ТОЛЬКО в scud_lockers
    # (postgres_locker). На прочих инстансах таблицы нет — подставляем литерал 0,
    # чтобы единый _sql не падал и не ронял остальные pg-метрики этих инстансов.
    _ob_pending='0'; _ob_oldest='0'; _ob_bytes='0'; _ob_retry='0'; _ob_dl='0'
    if [ "$HOST" = postgres_locker ]; then
        _ob_filt="delivered_at IS NULL AND dead_lettered_at IS NULL"
        _ob_pending="(SELECT count(*) FROM outbound_history_events WHERE ${_ob_filt})"
        _ob_oldest="COALESCE((SELECT CEIL(EXTRACT(epoch FROM now() - min(created_at)))::bigint FROM outbound_history_events WHERE ${_ob_filt}), 0)"
        _ob_bytes="COALESCE((SELECT sum(pg_column_size(payload))::bigint FROM outbound_history_events WHERE ${_ob_filt}), 0)"
        _ob_retry="(SELECT count(*) FROM outbound_history_events WHERE ${_ob_filt} AND attempt_count > 0)"
        _ob_dl="(SELECT count(*) FROM outbound_history_events WHERE dead_lettered_at IS NOT NULL)"
    fi
    _sql="SELECT (SELECT numbackends FROM pg_stat_database WHERE datname = current_database()),
                 (SELECT setting::int FROM pg_settings WHERE name = 'max_connections'),
                 (SELECT deadlocks FROM pg_stat_database WHERE datname = current_database()),
                 (SELECT xact_rollback FROM pg_stat_database WHERE datname = current_database()),
                 (SELECT COALESCE(CEIL(EXTRACT(epoch FROM max(now() - xact_start)))::bigint, 0) FROM pg_stat_activity WHERE state <> 'idle' AND xact_start IS NOT NULL),
                 (${_hpb}),
                 (${_ob_pending}), (${_ob_oldest}), (${_ob_bytes}), (${_ob_retry}), (${_ob_dl}),
                 (${_orphan})"
    _row="$(psql -h "$HOST" -p "$PG_PORT" -U "$PG_USER" -d "$_dbname" \
                -tA --no-psqlrc -v ON_ERROR_STOP=1 -c "$_sql" 2>/dev/null)" || return 1
    printf 'numbackends %s\n'             "$(printf '%s' "$_row" | cut -d'|' -f1)"
    printf 'max_connections %s\n'         "$(printf '%s' "$_row" | cut -d'|' -f2)"
    printf 'deadlocks %s\n'               "$(printf '%s' "$_row" | cut -d'|' -f3)"
    printf 'xact_rollback %s\n'           "$(printf '%s' "$_row" | cut -d'|' -f4)"
    printf 'longest_txn_seconds %s\n'     "$(printf '%s' "$_row" | cut -d'|' -f5)"
    printf 'history_partition_bytes %s\n' "$(printf '%s' "$_row" | cut -d'|' -f6)"
    printf 'outbox_pending %s\n'          "$(printf '%s' "$_row" | cut -d'|' -f7)"
    printf 'outbox_oldest_age_s %s\n'     "$(printf '%s' "$_row" | cut -d'|' -f8)"
    printf 'outbox_pending_bytes %s\n'    "$(printf '%s' "$_row" | cut -d'|' -f9)"
    printf 'outbox_retrying %s\n'         "$(printf '%s' "$_row" | cut -d'|' -f10)"
    printf 'outbox_deadletter %s\n'       "$(printf '%s' "$_row" | cut -d'|' -f11)"
    printf 'history_orphan_media %s\n'    "$(printf '%s' "$_row" | cut -d'|' -f12)"
}

_build() {
    case "$SOURCE" in
        pgb) build_pgb ;;
        pg)  build_pg ;;
        *)   return 1 ;;
    esac
}

cache_fresh() {  # <file>
    [ -f "$1" ] || return 1
    _mt="$(stat -c %Y "$1" 2>/dev/null || echo 0)"
    [ "$(( $(date +%s) - _mt ))" -lt "$CACHE_TTL" ]
}

_rebuild() {  # <cache_file> — build to a temp file and atomically move into place
    _tmp="$(mktemp "${1}.XXXXXX")" || return 1
    if _build > "$_tmp" 2>/dev/null && [ -s "$_tmp" ]; then
        mv "$_tmp" "$1"
    else
        rm -f "$_tmp"
        return 1
    fi
}

ensure_cache() {  # refresh the (source,host) cache if stale; serialise via flock
    _cf="$CACHE_DIR/${SOURCE}_${HOST}"
    mkdir -p "$CACHE_DIR" 2>/dev/null || true
    cache_fresh "$_cf" && { CACHE_FILE="$_cf"; return 0; }
    if exec 9>"${_cf}.lock" && flock -w 30 9; then
        # Another holder may have refreshed it while we waited.
        cache_fresh "$_cf" || _rebuild "$_cf" || return 1
    else
        _rebuild "$_cf" || return 1
    fi
    CACHE_FILE="$_cf"
}

case "$SOURCE" in
    pgb|pg) ;;
    *) exit 1 ;;
esac
# history_* exist ONLY for the history instance (template R5).
case "$METRIC" in
    history_*) [ "$HOST" = postgres_history ] || exit 1 ;;
esac
# outbox_* exist ONLY on the locker instance (outbound_history_events ∈ scud_lockers).
case "$METRIC" in
    outbox_*) [ "$HOST" = postgres_locker ] || exit 1 ;;
esac

ensure_cache || exit 1
_val="$(awk -v k="$METRIC" '$1 == k { print $2; found = 1 } END { exit !found }' "$CACHE_FILE")" || exit 1
emit_int "$_val"
