#!/usr/bin/env bash
# Проверка блоков схемы БД (deploy.sh: apply_schema_upgrades) на настоящем PostgreSQL.
# Нужен docker и git (для доступа к ревизии cc41adc нужна полная история: fetch-depth 0).
# Запуск из любого каталога репозитория:
#   bash backend/scripts/tests/test_schema_upgrade.sh
# На Windows (Git Bash) перед запуском: MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'.
#
# Сценарии для каждой из 6 БД (остальное — см. заголовки «==>»):
#   A  свежая БД из init-файла текущей ревизии (эталон)
#   B  A + apply_schema_upgrades дважды == A
#   C  БД из init-файла ревизии cc41adc + apply_schema_upgrades == A, кроме известных остатков
#   D  --baseline только отмечает блоки в журнале, не выполняя их
#   E  изменение блока вызывает повторное применение, ошибка блока не пишется в журнал
# SC2016: тела bash -c в одинарных кавычках; SC2030/2031: BACKEND_DIR подменяется в подоболочке; SC2317: функции вызываются косвенно.
# shellcheck disable=SC2016,SC2030,SC2031,SC2317

set -uo pipefail
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*'

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="$SCRIPT_DIR/../deploy.sh"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../.." && pwd)"
git_repo() { (cd "$REPO_ROOT" && git "$@"); }
FLOOR_REV=cc41adc
PG_IMAGE=${PG_IMAGE:-postgres:15-alpine}
CT="scud-schema-test-$$"
WORK="$(mktemp -d)"

# shellcheck source-path=SCRIPTDIR source=../deploy.sh
source "$DEPLOY_SH"
BACKEND_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

pass=0
fail=0
ok() {
    echo "  PASS: $1"
    pass=$((pass + 1))
}
err_t() {
    echo "  FAIL: $1" >&2
    fail=$((fail + 1))
}
check() {
    local name=$1
    shift
    if "$@"; then ok "$name"; else err_t "$name"; fi
}
show_diff() { diff -u "$1" "$2" | head -"${3:-40}" >&2; }

cleanup() {
    docker rm -f "$CT" >/dev/null 2>&1
    rm -rf "$WORK"
}
trap cleanup EXIT

pg() { docker exec -i -e PGOPTIONS="-c client_min_messages=warning" "$CT" psql -U postgres -q -X -v ON_ERROR_STOP=1 "$@"; }

# Подмена боевого db_psql: те же базы, но с суффиксом сценария и без compose.
SUF=a
db_psql() {
    local db=$2
    shift 2
    pg -d "${db}_${SUF}" "$@"
}

# Строки базы и пути init-файла из DB_MAP.
db_names() {
    local entry svc db rel req
    for entry in "${DB_MAP[@]}"; do
        IFS='|' read -r svc db rel req <<<"$entry"
        echo "$db"
    done
}
db_field() { # db поле(rel|req)
    local entry svc db rel req
    for entry in "${DB_MAP[@]}"; do
        IFS='|' read -r svc db rel req <<<"$entry"
        if [ "$db" = "$1" ]; then
            case $2 in rel) echo "$rel" ;; req) echo "$req" ;; esac
            return 0
        fi
    done
    return 1
}

create_dbs() { # суффикс источник(head|floor)
    local suf=$1 src=$2 db rel file
    for db in $(db_names); do
        rel=$(db_field "$db" rel)
        if [ "$src" = head ]; then file=$BACKEND_DIR/$rel; else file=$WORK/floor/$db.sql; fi
        pg -d postgres -c "DROP DATABASE IF EXISTS \"${db}_${suf}\"" -c "CREATE DATABASE \"${db}_${suf}\"" || return 1
        tr -d '\r' <"$file" | pg -d "${db}_${suf}" >/dev/null 2>"$WORK/init_err" || {
            echo "init $db/$src не применился:" >&2
            head -5 "$WORK/init_err" >&2
            return 1
        }
    done
}

dump_schema() { # база
    docker exec "$CT" pg_dump -U postgres -s --no-owner --no-privileges -T deploy_schema_upgrades -d "$1" |
        tr -d '\r' | grep -Ev '^--|^$|^SET |^SELECT pg_catalog.set_config|^\\(un)?restrict'
}

dump_data() { # база вид
    case $2 in
    scud_lockers) pg -At -d "$1" -c "SELECT code,name,description,unit,value_type,states FROM sensor_types ORDER BY code; SELECT enum_range(NULL::command_type);" ;;
    scud_notifications) pg -At -d "$1" -c "SELECT * FROM bot_poller_state" | cut -d'|' -f1,2 ;;
    scud_history) pg -At -d "$1" -c "SELECT enum_range(NULL::media_kind), enum_range(NULL::event_severity); SELECT version, description FROM schema_migrations" ;;
    *) echo none ;;
    esac
}

# Остатки старых БД, которые блоки не могут и не должны приводить к эталону
# (recon schema.md (d).6; все безвредны для кода сервисов):
#  - порядок колонок: ADD COLUMN добавляет в конец, поэтому строки сравниваются как
#    множество без хвостовых запятых;
#  - device_connections и DEFAULT 'ws' у lockers.transport: остались от WS-транспорта
#    (БД до его удаления), удаление в блоках не предусмотрено;
#  - значение media_kind 'video' и тексты COMMENT ON, которые блоки не переписывают.
benign_normalize() {
    awk '
        { buf = buf $0 "\n" }
        /;$/ { if (buf !~ /device_connections/) printf "%s", buf; buf = "" }
        END { printf "%s", buf }' |
        sed -E "s/,$//; s/ DEFAULT '(ws|mqtt)'::public\.locker_transport//; s/^( *)'video'$/\1/; s/,video\}/}/" |
        grep -Ev "^COMMENT ON |^ *$" |
        sort
}

block_count() { schema_block_names "$BACKEND_DIR/$(db_field "$1" rel)" 2>/dev/null | grep -c . ; }

journal_names() { pg -At -d "$1" -c "SELECT name FROM deploy_schema_upgrades ORDER BY name"; }

echo "==> Подготовка: контейнер $PG_IMAGE и файлы ревизии $FLOOR_REV"
if ! git_repo cat-file -e "$FLOOR_REV^{commit}" 2>/dev/null; then
    echo "ОШИБКА: ревизия $FLOOR_REV не найдена; нужна полная история (git fetch --unshallow / fetch-depth: 0)." >&2
    exit 1
fi
mkdir -p "$WORK/floor"
for db in $(db_names); do
    git_repo show "$FLOOR_REV:backend/$(db_field "$db" rel)" >"$WORK/floor/$db.sql" || {
        echo "ОШИБКА: нет файла $(db_field "$db" rel) в $FLOOR_REV" >&2
        exit 1
    }
done
if ! docker run -d --name "$CT" -e POSTGRES_HOST_AUTH_METHOD=trust "$PG_IMAGE" >/dev/null; then
    echo "ОШИБКА: не удалось запустить $PG_IMAGE" >&2
    exit 1
fi
ready=0
for _ in $(seq 1 100); do
    if docker logs "$CT" 2>&1 | grep -q 'init process complete' && docker exec "$CT" pg_isready -U postgres -q 2>/dev/null; then
        ready=1
        break
    fi
    sleep 1
done
if [ "$ready" != 1 ]; then
    echo "ОШИБКА: PostgreSQL не запустился за 100 с" >&2
    docker logs "$CT" 2>&1 | tail -20 >&2
    exit 1
fi

# ---------------------------------------------------------------------------
echo "==> Тест 1: разметка блоков в init-файлах"
declare -A EXPECT=(
    [scud_lockers]="sensor-type-states outbox-dead-letter relay-photo-series relay-control"
    [scud_history]="media-security-event-uid"
    [scud_notifications]="outbound-max-body-nullable outbound-max-channel"
    [scud_users]=""
    [scud_auth]=""
    [scud_reports]=""
)
for db in $(db_names); do
    got=$(schema_block_names "$BACKEND_DIR/$(db_field "$db" rel)" 2>/dev/null | tr '\n' ' ')
    got=${got% }
    if [ "$got" = "${EXPECT[$db]}" ]; then ok "$db: блоки '${EXPECT[$db]}'"; else err_t "$db: ожидались блоки '${EXPECT[$db]}', получено '$got'"; fi
    if [ "$(db_field "$db" req)" = 1 ]; then
        check "$db: помечен как обязательный к наличию блоков (>=1)" test -n "$got"
    fi
done

echo "==> Тест 2: CRLF не меняет блоки и контрольные суммы"
for db in scud_lockers scud_history scud_notifications; do
    src=$BACKEND_DIR/$(db_field "$db" rel)
    sed 's/$/\r/' "$src" >"$WORK/crlf.sql"
    check "$db: блоки CRLF == LF" bash -c 'diff <(source "$1"; schema_blocks "$2") <(source "$1"; schema_blocks "$3") >/dev/null' _ "$DEPLOY_SH" "$src" "$WORK/crlf.sql"
    n=$(schema_block_names "$src" | head -1)
    check "$db: контрольная сумма CRLF == LF" test "$(block_checksum "$n" "$src")" = "$(block_checksum "$n" "$WORK/crlf.sql")"
done

# ---------------------------------------------------------------------------
echo "==> A: свежие БД из init-файлов текущей ревизии"
SUF=a
if create_dbs a head; then
    ok "6 БД созданы из init-файлов"
else
    err_t "A: init-файлы текущей ревизии не применились"
    echo "Итого: пройдено $pass, провалено $((fail))"
    exit 1
fi
for db in $(db_names); do
    dump_schema "${db}_a" >"$WORK/A_$db.schema"
    dump_data "${db}_a" "$db" >"$WORK/A_$db.data"
done

# ---------------------------------------------------------------------------
echo "==> B: свежая БД + блоки дважды == свежая БД"
SUF=b
create_dbs b head || err_t "B: init не применился"
for run in 1 2; do
    if apply_schema_upgrades >"$WORK/B_run$run.log" 2>&1; then ok "apply_schema_upgrades, запуск $run: код 0"; else
        err_t "apply_schema_upgrades, запуск $run: ненулевой код"
        tail -15 "$WORK/B_run$run.log" >&2
    fi
done
check "запуск 1 применил блоки" grep -q '^ *+ .*: применён' "$WORK/B_run1.log"
check "запуск 2 ничего не применил" bash -c '! grep -q "^ *[+~] " "$1" && grep -q "уже применён" "$1"' _ "$WORK/B_run2.log"
check "проба: sensor-type-states на БД с sensor_types.states отмечен без выполнения" \
    grep -q "+ sensor-type-states: отмечен как применённый без выполнения" "$WORK/B_run1.log"
for db in $(db_names); do
    dump_schema "${db}_b" >"$WORK/B_$db.schema"
    dump_data "${db}_b" "$db" >"$WORK/B_$db.data"
    if cmp -s "$WORK/A_$db.schema" "$WORK/B_$db.schema"; then ok "$db: схема после блоков x2 == свежая"; else
        err_t "$db: схема после блоков x2 != свежая"
        show_diff "$WORK/A_$db.schema" "$WORK/B_$db.schema"
    fi
    if cmp -s "$WORK/A_$db.data" "$WORK/B_$db.data"; then ok "$db: данные справочников == свежие"; else
        err_t "$db: данные справочников после блоков != свежие"
        show_diff "$WORK/A_$db.data" "$WORK/B_$db.data"
    fi
    want=$(schema_block_names "$BACKEND_DIR/$(db_field "$db" rel)" 2>/dev/null | sort)
    got=$(journal_names "${db}_b")
    if [ "$want" = "$got" ]; then ok "$db: журнал содержит ровно блоки файла"; else err_t "$db: журнал '$got', ожидалось '$want'"; fi
done

# ---------------------------------------------------------------------------
echo "==> B2: колонка states есть, у intrusion подписей нет (БД 2026-10-05..10-13)"
SUF=b2
create_dbs b2 head || err_t "B2: init не применился"
pg -d scud_lockers_b2 -c "UPDATE sensor_types SET states = NULL WHERE code = 'intrusion'"
if apply_schema_upgrades >"$WORK/B2_run.log" 2>&1; then ok "B2: apply_schema_upgrades: код 0"; else
    err_t "B2: apply_schema_upgrades: ненулевой код"
    tail -15 "$WORK/B2_run.log" >&2
fi
check "проба: без states у intrusion блок sensor-type-states выполнен" \
    grep -q "+ sensor-type-states: применён" "$WORK/B2_run.log"
check "B2: подписи intrusion восстановлены" \
    test "$(pg -At -d scud_lockers_b2 -c "SELECT states IS NOT NULL FROM sensor_types WHERE code = 'intrusion'")" = t

# ---------------------------------------------------------------------------
echo "==> C: БД ревизии $FLOOR_REV + блоки == свежая БД (кроме известных остатков)"
SUF=c
if ! create_dbs c floor; then
    err_t "C: init-файлы ревизии $FLOOR_REV не применились"
else
    ok "6 БД созданы из файлов ревизии $FLOOR_REV"
    for db in $(db_names); do
        dump_schema "${db}_c" >"$WORK/C0_$db.schema"
    done
    check "locker: до блоков схема действительно старая" bash -c '! cmp -s "$1" "$2"' _ "$WORK/A_scud_lockers.schema" "$WORK/C0_scud_lockers.schema"
    for run in 1 2; do
        if apply_schema_upgrades >"$WORK/C_run$run.log" 2>&1; then ok "apply_schema_upgrades на старых БД, запуск $run: код 0"; else
            err_t "apply_schema_upgrades на старых БД, запуск $run: ненулевой код"
            tail -15 "$WORK/C_run$run.log" >&2
        fi
    done
    check "проба: на БД без sensor_types.states блок sensor-type-states выполнен" \
        grep -q "+ sensor-type-states: применён" "$WORK/C_run1.log"
    for db in $(db_names); do
        dump_schema "${db}_c" >"$WORK/C_$db.schema"
        dump_data "${db}_c" "$db" >"$WORK/C_$db.data"
        case $db in
        scud_lockers | scud_history)
            benign_normalize <"$WORK/A_$db.schema" >"$WORK/A_$db.norm"
            benign_normalize <"$WORK/C_$db.schema" >"$WORK/C_$db.norm"
            benign_normalize <"$WORK/A_$db.data" >"$WORK/A_$db.dnorm"
            benign_normalize <"$WORK/C_$db.data" >"$WORK/C_$db.dnorm"
            s_ref=$WORK/A_$db.norm s_new=$WORK/C_$db.norm d_ref=$WORK/A_$db.dnorm d_new=$WORK/C_$db.dnorm
            ;;
        *)
            s_ref=$WORK/A_$db.schema s_new=$WORK/C_$db.schema d_ref=$WORK/A_$db.data d_new=$WORK/C_$db.data
            ;;
        esac
        if cmp -s "$s_ref" "$s_new"; then ok "$db: схема старой БД после блоков == свежая"; else
            err_t "$db: схема старой БД после блоков != свежая"
            show_diff "$s_ref" "$s_new"
        fi
        if cmp -s "$d_ref" "$d_new"; then ok "$db: данные справочников старой БД == свежие"; else
            err_t "$db: данные справочников старой БД != свежие"
            show_diff "$d_ref" "$d_new"
        fi
    done
fi

# ---------------------------------------------------------------------------
echo "==> D: --baseline отмечает блоки, не выполняя их"
SUF=d
create_dbs d floor || err_t "D: init старых БД не применился"
for db in $(db_names); do dump_schema "${db}_d" >"$WORK/D0_$db.schema"; done
if apply_schema_upgrades --baseline >"$WORK/D_base.log" 2>&1; then ok "--baseline: код 0"; else
    err_t "--baseline: ненулевой код"
    tail -15 "$WORK/D_base.log" >&2
fi
check "--baseline ничего не выполнял (в логе нет '+ имя: применён')" bash -c '! grep -Eq "^ *\+ .*: применён$" "$1"' _ "$WORK/D_base.log"
for db in $(db_names); do
    dump_schema "${db}_d" >"$WORK/D1_$db.schema"
    check "$db: схема после --baseline не изменилась" cmp -s "$WORK/D0_$db.schema" "$WORK/D1_$db.schema"
    want=$(schema_block_names "$BACKEND_DIR/$(db_field "$db" rel)" 2>/dev/null | sort)
    got=$(journal_names "${db}_d")
    if [ "$want" = "$got" ]; then ok "$db: журнал после --baseline содержит все блоки"; else err_t "$db: журнал '$got', ожидалось '$want'"; fi
done
apply_schema_upgrades >"$WORK/D_run.log" 2>&1
check "обычный запуск после --baseline: код 0" test $? -eq 0
check "после --baseline ничего не применяется" bash -c '! grep -q "^ *[+~] " "$1" && grep -q "уже применён" "$1"' _ "$WORK/D_run.log"
dump_schema scud_lockers_d >"$WORK/D2_locker.schema"
check "locker после --baseline и запуска по-прежнему старый" cmp -s "$WORK/D0_scud_lockers.schema" "$WORK/D2_locker.schema"

# ---------------------------------------------------------------------------
echo "==> E: изменённый блок применяется заново, сбойный блок не попадает в журнал"
SUF=b
ETREE=$WORK/etree
for db in $(db_names); do
    rel=$(db_field "$db" rel)
    mkdir -p "$ETREE/$(dirname "$rel")"
    cp "$BACKEND_DIR/$rel" "$ETREE/$rel"
done
LOCKER_REL=$(db_field scud_lockers rel)
BLK=relay-control
old_sum=$(pg -At -d scud_lockers_b -c "SELECT checksum FROM deploy_schema_upgrades WHERE name = '$BLK'")
awk -v end="-- $BLK:end" '$0 ~ "^" end "\r?$" { print "CREATE TABLE IF NOT EXISTS schema_test_marker (i int);" } { print }' \
    "$BACKEND_DIR/$LOCKER_REL" >"$ETREE/$LOCKER_REL"
if (
    BACKEND_DIR=$ETREE
    apply_schema_upgrades >"$WORK/E_run1.log" 2>&1
); then ok "запуск с изменённым блоком: код 0"; else
    err_t "запуск с изменённым блоком: ненулевой код"
    tail -15 "$WORK/E_run1.log" >&2
fi
check "лог: '~ $BLK' — блок изменился" grep -Eq "^ *~ $BLK:" "$WORK/E_run1.log"
check "лог: остальные блоки не перезапускались" bash -c '! grep -E "^ *[+~] " "$1" | grep -qv "$2"' _ "$WORK/E_run1.log" "$BLK"
check "изменённый блок действительно выполнился" test "$(pg -At -d scud_lockers_b -c "SELECT to_regclass('schema_test_marker') IS NOT NULL")" = t
new_sum=$(pg -At -d scud_lockers_b -c "SELECT checksum FROM deploy_schema_upgrades WHERE name = '$BLK'")
check "контрольная сумма в журнале обновилась" test -n "$new_sum" -a "$new_sum" != "$old_sum"
(
    BACKEND_DIR=$ETREE
    apply_schema_upgrades >"$WORK/E_run2.log" 2>&1
)
check "повторный запуск с тем же блоком ничего не применяет" bash -c '! grep -q "^ *[+~] " "$1"' _ "$WORK/E_run2.log"

awk -v end="-- $BLK:end" '$0 ~ "^" end "\r?$" { print "SELECT 1/0;" } { print }' "$BACKEND_DIR/$LOCKER_REL" >"$ETREE/$LOCKER_REL"
(
    BACKEND_DIR=$ETREE
    apply_schema_upgrades >"$WORK/E_bad.log" 2>&1
)
check "сбойный блок: ненулевой код" test $? -ne 0
check "сбойный блок: журнал хранит прежнюю сумму" test "$(pg -At -d scud_lockers_b -c "SELECT checksum FROM deploy_schema_upgrades WHERE name = '$BLK'")" = "$new_sum"
check "сбойный блок: ошибка названа в выводе" grep -q "$BLK" "$WORK/E_bad.log"

# ---------------------------------------------------------------------------
echo
echo "Итого: пройдено $pass, провалено $fail"
[ "$fail" -eq 0 ]
