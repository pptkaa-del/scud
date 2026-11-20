#!/bin/bash
# Юнит-тесты backend/scripts/deploy.sh (docker, curl и psql — моки).
# Запуск (репозиторий монтируется только на чтение; git нужен для теста pack):
#   docker run --rm -v <repo>:/repo:ro bash:5 sh -c \
#     'apk add --no-cache git coreutils openssl >/dev/null && bash /repo/backend/scripts/tests/test_deploy.sh'
# Без доступа к пакетам Alpine — в любом образе с bash и git:
#   docker run --rm -v <repo>:/repo:ro python:3.12 bash /repo/backend/scripts/tests/test_deploy.sh
#
# deploy.sh подключается через source: main не вызывается, а set -euo pipefail
# включается только внутри main. Здесь -e не включаем, чтобы упавшая проверка
# не обрывала остальные; функции, которые завершают процесс (die), вызываются
# в подоболочке.
# SC2016: тела bash -c намеренно в одинарных кавычках; SC2317: моки вызываются косвенно.
# shellcheck disable=SC2016,SC2317

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEPLOY_SH="$SCRIPT_DIR/../deploy.sh"
REPO_BACKEND="$(cd "$SCRIPT_DIR/../.." && pwd)"

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
check_eq() {
    if [ "$2" = "$3" ]; then ok "$1"; else err_t "$1: ожидалось '$3', получено '$2'"; fi
}

TROOT=$(mktemp -d)
trap 'rm -rf "$TROOT"' EXIT

opts_before=$-
# shellcheck source-path=SCRIPTDIR source=../deploy.sh
source "$DEPLOY_SH"

echo "==> Тест 1: source без побочных эффектов"
check "set -e не включился" test "$opts_before" = "$-"
check "функции определены" declare -F schema_blocks apply_schema_upgrades db_psql pack_release >/dev/null
check "26 имён секретов" test "${#SECRET_NAMES[@]}" -eq 26
sms_listed=1
for name in sms_api_user sms_api_password wg_private_key wg_preshared_key; do
    [[ " ${SECRET_NAMES[*]} " == *" $name "* ]] || sms_listed=0
done
check "SMS-секреты в списке" test "$sms_listed" = 1
check "6 баз" test "${#DB_MAP[@]}" -eq 6

parse_args
reset_conf
SECRET_ADMIN_PASSWORD=""
SECRET_MAX_BOT_TOKEN=""

# ---------------------------------------------------------------------------
echo "==> Тест 2: разбор блоков схемы"
F=$TROOT/blocks.sql
cat >"$F" <<'SQL'
CREATE TABLE a (id int);
-- first-block:begin
ALTER TABLE a ADD COLUMN IF NOT EXISTS b int;
-- first-block:end
SELECT 1;
-- second-2:begin
CREATE INDEX IF NOT EXISTS ix ON a (b);
-- second-2:end
SQL
check_eq "имена по порядку" "$(schema_block_names "$F" | tr '\n' ' ')" "first-block second-2 "
check_eq "все блоки подряд" "$(schema_blocks "$F" | wc -l | tr -d ' ')" 6
check_eq "один блок" "$(schema_block second-2 "$F" | sed -n 2p)" "CREATE INDEX IF NOT EXISTS ix ON a (b);"
check "нет блока -> код 1" bash -c 'source "$1"; schema_block nope "$2" >/dev/null; [ $? -eq 1 ]' _ "$DEPLOY_SH" "$F"

FC=$TROOT/blocks-crlf.sql
sed 's/$/\r/' "$F" >"$FC"
check_eq "CRLF: те же имена" "$(schema_block_names "$FC" | tr '\n' ' ')" "first-block second-2 "
check_eq "CRLF: та же контрольная сумма" "$(block_checksum first-block "$FC")" "$(block_checksum first-block "$F")"
out=$(schema_blocks "$FC")
check "CRLF: в выводе нет CR" test "$out" = "$(schema_blocks "$F")"

FN=$TROOT/none.sql
printf 'CREATE TABLE x (id int);\n-- просто комментарий\n' >"$FN"
rc=0
out=$(schema_blocks "$FN") || rc=$?
check_eq "нет блоков: код 1" "$rc" 1
check_eq "нет блоков: пустой вывод" "$out" ""
rc=0
schema_block_names "$FN" >/dev/null || rc=$?
check_eq "нет блоков: names код 1" "$rc" 1

FB=$TROOT/bad.sql
printf -- '-- a:begin\nSELECT 1;\n' >"$FB"
rc=0
schema_blocks "$FB" >/dev/null 2>&1 || rc=$?
check_eq "незакрытый блок: код 2" "$rc" 2
printf -- '-- a:begin\n-- b:begin\n-- b:end\n-- a:end\n' >"$FB"
rc=0
schema_block_names "$FB" >/dev/null 2>&1 || rc=$?
check_eq "вложенный блок: код 2" "$rc" 2
printf -- '-- a:begin\n-- a:end\n-- a:begin\n-- a:end\n' >"$FB"
rc=0
schema_block_names "$FB" >/dev/null 2>&1 || rc=$?
check_eq "повтор имени: код 2" "$rc" 2
printf -- '-- a:begin\n-- b:end\n' >"$FB"
rc=0
schema_blocks "$FB" >/dev/null 2>&1 || rc=$?
check_eq "чужой end: код 2" "$rc" 2
printf -- '-- Upper:begin\n-- Upper:end\n' >"$FB"
rc=0
schema_blocks "$FB" >/dev/null 2>&1 || rc=$?
check_eq "имя вне [a-z0-9-] — не маркер: код 1" "$rc" 1

for entry in "${DB_MAP[@]}"; do
    IFS='|' read -r _svc db rel req <<<"$entry"
    if [ "$req" = 1 ]; then
        n=$(schema_block_names "$REPO_BACKEND/$rel" 2>/dev/null | wc -l | tr -d ' ')
        check "в репозитории $db есть блоки ($n)" test "$n" -ge 1
    else
        rc=0
        schema_block_names "$REPO_BACKEND/$rel" >/dev/null 2>&1 || rc=$?
        check "в репозитории $db разметка корректна (rc=$rc)" test "$rc" -ne 2
    fi
done

# ---------------------------------------------------------------------------
echo "==> Тест 3: apply_schema_upgrades и журнал"
FAKE_DB=$TROOT/fakedb
mkdir -p "$FAKE_DB"
# Журнал каждой БД — файл «имя checksum»; блоки пишутся в calls.
db_psql() {
    local db=$2 sql n="" c="" journal
    shift 2
    sql=$(cat)
    while [ "$#" -gt 0 ]; do
        if [ "$1" = -v ]; then
            case "$2" in
                n=*) n=${2#n=} ;;
                c=*) c=${2#c=} ;;
            esac
            shift
        fi
        shift
    done
    journal=$FAKE_DB/$db.journal
    case "$sql" in
        *"CREATE TABLE IF NOT EXISTS deploy_schema_upgrades"*) touch "$journal" ;;
        *"SELECT name || ' ' || checksum"*) cat "$journal" ;;
        *"INSERT INTO deploy_schema_upgrades"*)
            grep -v "^$n " "$journal" >"$journal.tmp"
            printf '%s %s\n' "$n" "$c" >>"$journal.tmp"
            mv "$journal.tmp" "$journal"
            ;;
        *"probe-extra"*)
            if [ -f "$FAKE_DB/probe_yes" ]; then
                echo 1
            fi
            ;;
        *)
            n=$(printf '%s\n' "$sql" | sed -n '1s/^-- \(.*\):begin$/\1/p')
            echo "$db $n" >>"$FAKE_DB/calls"
            if [ "$n" = "${FAKE_FAIL_BLOCK:-}" ]; then
                return 3
            fi
            ;;
    esac
}

make_tree() {
    local base=$1 entry svc db rel req
    for entry in "${DB_MAP[@]}"; do
        IFS='|' read -r svc db rel req <<<"$entry"
        mkdir -p "$base/$(dirname "$rel")"
        printf 'CREATE TABLE t_%s (id int);\n' "$db" >"$base/$rel"
        if [ "$req" = 1 ]; then
            printf -- '-- %s-one:begin\nSELECT 1;\n-- %s-one:end\n' "${db//_/-}" "${db//_/-}" >>"$base/$rel"
        fi
    done
    printf -- '-- extra:begin\nSELECT 2;\n-- extra:end\n' >>"$base/services/locker_api/app/locker_data/db/migrations/001_init.sql"
}

set_root "$TROOT/inst"
make_tree "$BACKEND_DIR"
LOCKER_SQL=$BACKEND_DIR/services/locker_api/app/locker_data/db/migrations/001_init.sql

apply_schema_upgrades >"$TROOT/out1" 2>&1
check_eq "первый запуск: код 0" "$?" 0
check_eq "первый запуск: выполнено 4 блока" "$(wc -l <"$FAKE_DB/calls" | tr -d ' ')" 4
check "locker: блоки в порядке файла" test "$(grep scud_lockers "$FAKE_DB/calls" | tr '\n' ' ')" = "scud_lockers scud-lockers-one scud_lockers extra "
check "журнал есть во всех 6 БД" test "$(find "$FAKE_DB" -name '*.journal' | wc -l | tr -d ' ')" -eq 6
check_eq "журнал locker: 2 записи" "$(wc -l <"$FAKE_DB/scud_lockers.journal" | tr -d ' ')" 2
check_eq "контрольная сумма записана" "$(awk '$1 == "extra" { print $2 }' "$FAKE_DB/scud_lockers.journal")" "$(block_checksum extra "$LOCKER_SQL")"

: >"$FAKE_DB/calls"
apply_schema_upgrades >"$TROOT/out2" 2>&1
check_eq "повторный запуск: код 0" "$?" 0
check_eq "повторный запуск: ничего не выполнено" "$(wc -l <"$FAKE_DB/calls" | tr -d ' ')" 0
check "повторный запуск: сообщение «уже применён»" grep -q "= extra: уже применён" "$TROOT/out2"

sed -i 's/SELECT 2;/SELECT 3;/' "$LOCKER_SQL"
apply_schema_upgrades >"$TROOT/out3" 2>&1
check_eq "изменённый блок: выполнен повторно только он" "$(tr '\n' ' ' <"$FAKE_DB/calls")" "scud_lockers extra "
check "изменённый блок: сообщение" grep -q "~ extra" "$TROOT/out3"
check_eq "изменённый блок: новая сумма в журнале" "$(awk '$1 == "extra" { print $2 }' "$FAKE_DB/scud_lockers.journal")" "$(block_checksum extra "$LOCKER_SQL")"

: >"$FAKE_DB/calls"
rm -f "$FAKE_DB"/*.journal
apply_schema_upgrades --baseline >"$TROOT/out4" 2>&1
check_eq "baseline: код 0" "$?" 0
check_eq "baseline: ничего не выполнено" "$(wc -l <"$FAKE_DB/calls" | tr -d ' ')" 0
check_eq "baseline: все 4 блока в журналах" "$(cat "$FAKE_DB"/*.journal | wc -l | tr -d ' ')" 4

: >"$FAKE_DB/calls"
rm -f "$FAKE_DB"/*.journal
sed -i 's/SELECT 3;/SELECT 4;/' "$LOCKER_SQL"
rc=0
FAKE_FAIL_BLOCK=extra apply_schema_upgrades >"$TROOT/out5" 2>&1 || rc=$?
check "упавший блок: ненулевой код" test "$rc" -ne 0
check "упавший блок: не записан в журнал" bash -c '! grep -q "^extra " "$1"' _ "$FAKE_DB/scud_lockers.journal"
check "упавший блок: предыдущий записан" grep -q "^scud-lockers-one " "$FAKE_DB/scud_lockers.journal"
check "упавший блок: следующие БД не тронуты" bash -c '! grep -q scud_history "$1"' _ "$FAKE_DB/calls"

probes_saved=("${LEGACY_PROBES[@]}")
LEGACY_PROBES=("scud_lockers|extra|SELECT 'probe-extra';")
: >"$FAKE_DB/calls"
rm -f "$FAKE_DB"/*.journal
touch "$FAKE_DB/probe_yes"
apply_schema_upgrades >"$TROOT/out7" 2>&1
check_eq "проба: код 0" "$?" 0
check "проба: блок без записи в журнале, но уже в БД — не выполнен" bash -c '! grep -q "scud_lockers extra" "$1"' _ "$FAKE_DB/calls"
check "проба: блок записан в журнал" grep -q "^extra $(block_checksum extra "$LOCKER_SQL")$" "$FAKE_DB/scud_lockers.journal"
check "проба: сообщение" grep -q "+ extra: отмечен как применённый без выполнения" "$TROOT/out7"
check "проба: блоки без пробы выполнены" grep -q "^scud_lockers scud-lockers-one$" "$FAKE_DB/calls"
sed -i 's/SELECT 4;/SELECT 5;/' "$LOCKER_SQL"
: >"$FAKE_DB/calls"
apply_schema_upgrades >/dev/null 2>&1
check "проба не применяется к изменённому блоку из журнала" grep -q "^scud_lockers extra$" "$FAKE_DB/calls"
rm -f "$FAKE_DB/probe_yes" "$FAKE_DB"/*.journal
: >"$FAKE_DB/calls"
apply_schema_upgrades >/dev/null 2>&1
check "проба отрицательна: блок выполнен" grep -q "^scud_lockers extra$" "$FAKE_DB/calls"
: >"$FAKE_DB/calls"
apply_schema_upgrades --baseline >/dev/null 2>&1
check "проба при --baseline не нужна: ничего не выполнено" test ! -s "$FAKE_DB/calls"
LEGACY_PROBES=("${probes_saved[@]}")
check "проба sensor-type-states объявлена" test -n "$(legacy_probe scud_lockers sensor-type-states)"
for entry in "${LEGACY_PROBES[@]}"; do
    IFS='|' read -r pdb pname _sql <<<"$entry"
    prel=""
    for e2 in "${DB_MAP[@]}"; do
        IFS='|' read -r _s d2 r2 _q <<<"$e2"
        [ "$d2" = "$pdb" ] && prel=$r2
    done
    check "проба $pdb/$pname: блок есть в файле репозитория" bash -c 'source "$1"; schema_block "$2" "$3" >/dev/null' _ "$DEPLOY_SH" "$pname" "$REPO_BACKEND/$prel"
done

HIST_SQL=$BACKEND_DIR/services/history_api/app/history_data/db/migrations/001_init_history.sql
printf 'CREATE TABLE only (id int);\n' >"$HIST_SQL"
rc=0
apply_schema_upgrades >"$TROOT/out6" 2>&1 || rc=$?
check "обязательный файл без блоков: ошибка" test "$rc" -ne 0
check "обязательный файл без блоков: сообщение" grep -q "не найдено ни одного блока" "$TROOT/out6"

journal_text=$(printf 'scud-lockers-one %s\nextra deadbeef\n' "$(block_checksum scud-lockers-one "$LOCKER_SQL")")
check_eq "schema_status" "$(schema_status "$LOCKER_SQL" "$journal_text" | tr '\n' ';')" "scud-lockers-one применён;extra изменён;"
check_eq "schema_status: пустой журнал" "$(schema_status "$LOCKER_SQL" "" | tr '\n' ';')" "scud-lockers-one ожидает;extra ожидает;"
unset -f db_psql

# ---------------------------------------------------------------------------
echo "==> Тест 4: KEY=VALUE и слияние .env"
E=$TROOT/kv.env
cat >"$E" <<'ENV'
# комментарий оператора
APP_ENV=development
export LOGIN_RATE_LIMIT=5
QUOTED='a b c'
DQ="x y"
INLINE=val # хвост
EMPTY=
ENV
check_eq "kv_get простое" "$(kv_get "$E" APP_ENV)" development
check_eq "kv_get export" "$(kv_get "$E" LOGIN_RATE_LIMIT)" 5
check_eq "kv_get одинарные кавычки" "$(kv_get "$E" QUOTED)" "a b c"
check_eq "kv_get двойные кавычки" "$(kv_get "$E" DQ)" "x y"
check_eq "kv_get комментарий в строке" "$(kv_get "$E" INLINE)" val
check "kv_get пустое значение есть" kv_has "$E" EMPTY
check "kv_get нет ключа -> 1" bash -c '! (source "$1"; kv_get "$2" NOPE)' _ "$DEPLOY_SH" "$E"
kv_set "$E" TRUSTED_PROXY_CIDR "10.0.0.1 10.0.0.0/24"
check_eq "kv_set с пробелами -> чтение" "$(kv_get "$E" TRUSTED_PROXY_CIDR)" "10.0.0.1 10.0.0.0/24"
kv_set "$E" APP_ENV production
check_eq "kv_set заменяет" "$(kv_get "$E" APP_ENV)" production
check_eq "kv_set не дублирует" "$(grep -c '^APP_ENV=' "$E")" 1
check "kv_set сохраняет комментарии" grep -q '^# комментарий оператора$' "$E"
check "kv_set_default не перезаписывает" bash -c '! (source "$1"; kv_set_default "$2" APP_ENV other)' _ "$DEPLOY_SH" "$E"
check_eq "kv_set_default: значение прежнее" "$(kv_get "$E" APP_ENV)" production
check "kv_quote отвергает кавычку" bash -c '! (source "$1"; kv_quote "a'"'"'b")' _ "$DEPLOY_SH"
kv_set "$E" CORS_ALLOWED_ORIGIN '*'
check_eq "kv_set '*'" "$(kv_get "$E" CORS_ALLOWED_ORIGIN)" '*'
check "kv_set: временных файлов не осталось" test -z "$(find "$TROOT" -maxdepth 1 -name 'kv.env.tmp.*')"
AWK_BIN=$TROOT/awkbin
mkdir -p "$AWK_BIN"
cat >"$AWK_BIN/awk" <<'MOCK'
#!/bin/sh
m=$(stat -L -c %a "/proc/$$/fd/1")
echo "$m" >"$AWK_MODE_LOG"
exit 1
MOCK
chmod +x "$AWK_BIN/awk"
kv_before=$(cat "$E")
rc=0
AWK_MODE_LOG=$TROOT/awk.mode PATH="$AWK_BIN:$PATH" kv_set "$E" APP_ENV other || rc=$?
check "kv_set: сбой awk -> ошибка" test "$rc" -ne 0
check_eq "kv_set: временный файл создаётся с правами 0600" "$(cat "$TROOT/awk.mode" 2>/dev/null)" 600
check "kv_set: после сбоя временный файл удалён" test -z "$(find "$TROOT" -maxdepth 1 -name 'kv.env.tmp.*')"
check_eq "kv_set: после сбоя файл не изменён" "$(cat "$E")" "$kv_before"

set_root "$TROOT/envroot"
mkdir -p "$SECRETS_DIR"
printf 'redissecret' >"$SECRETS_DIR/redis_password"
printf 'mqttsecret\n' >"$SECRETS_DIR/mqtt_password"
cat >"$ENV_FILE" <<'ENV'
APP_ENV=development
LOGIN_RATE_LIMIT=5
MQTT_PASSWORD=old-local
ENV
env_merge_defaults install
check_eq "merge: APP_ENV не перезаписан" "$(kv_get "$ENV_FILE" APP_ENV)" development
check_eq "merge: LOGIN_RATE_LIMIT не перезаписан" "$(kv_get "$ENV_FILE" LOGIN_RATE_LIMIT)" 5
check_eq "merge: MQTT_PASSWORD не перезаписан" "$(kv_get "$ENV_FILE" MQTT_PASSWORD)" old-local
check_eq "merge: добавлен VERIFY_RATE_LIMIT=10" "$(kv_get "$ENV_FILE" VERIFY_RATE_LIMIT)" 10
check_eq "merge: FRONTEND_PATH из каталога установки" "$(kv_get "$ENV_FILE" FRONTEND_PATH)" "$ROOT_DIR/frontend"
check_eq "merge: REDIS_PASSWORD = секрет" "$(kv_get "$ENV_FILE" REDIS_PASSWORD)" redissecret
check "merge: ENV_ADDED без существующих" bash -c '[[ " $1 " != *" APP_ENV "* ]] && [[ " $1 " == *" REDIS_PASSWORD "* ]]' _ "${ENV_ADDED[*]}"
check_eq "merge: .env 0600" "$(stat -c %a "$ENV_FILE")" 600
before=$(cat "$ENV_FILE")
env_merge_defaults install
check_eq "merge: повтор ничего не меняет" "$(cat "$ENV_FILE")" "$before"
check_eq "merge: повтор ничего не добавил" "${#ENV_ADDED[@]}" 0

printf 'APP_ENV=production\n' >"$ENV_FILE"
env_merge_defaults update
check_eq "update-merge: совместимое умолчание LOGIN_RATE_LIMIT=100" "$(kv_get "$ENV_FILE" LOGIN_RATE_LIMIT)" 100
check_eq "update-merge: FRONTEND_PATH как в compose" "$(kv_get "$ENV_FILE" FRONTEND_PATH)" /root/frontend
check_eq "update-merge: CORS как в compose" "$(kv_get "$ENV_FILE" CORS_ALLOWED_ORIGIN)" '*'
check_eq "update-merge: APP_ENV прежний" "$(kv_get "$ENV_FILE" APP_ENV)" production
check_eq "env_cur: из .env" "$(env_cur APP_ENV install)" production
rm -f "$ENV_FILE"
check_eq "env_cur: умолчание install" "$(env_cur APP_ENV install)" production
check_eq "env_cur: умолчание update" "$(env_cur APP_ENV update)" development

printf 'REDIS_PASSWORD=other\n' >"$ENV_FILE"
check "redis: предупреждение о несовпадении" bash -c 'source "$1"; set_root "$2"; check_redis_mirror 2>&1 | grep -q "не совпадает"' _ "$DEPLOY_SH" "$ROOT_DIR"

# ---------------------------------------------------------------------------
echo "==> Тест 5: секреты"
set_root "$TROOT/secroot"
mkdir -p "$SECRETS_DIR"
printf 'keep-admin-pass' >"$SECRETS_DIR/admin_password"
printf 'existing-jwt' >"$SECRETS_DIR/jwt_secret"
chmod 0600 "$SECRETS_DIR/jwt_secret"
mkdir "$SECRETS_DIR/redis_password"
ensure_secrets >/dev/null
check_eq "существующий admin_password не тронут" "$(cat "$SECRETS_DIR/admin_password")" keep-admin-pass
check_eq "существующий jwt_secret не тронут" "$(cat "$SECRETS_DIR/jwt_secret")" existing-jwt
check_eq "права существующего исправлены на 0644" "$(stat -c %a "$SECRETS_DIR/jwt_secret")" 644
check_eq "каталог секретов 0700" "$(stat -c %a "$SECRETS_DIR")" 700
check "пустой каталог на месте секрета заменён файлом" test -f "$SECRETS_DIR/redis_password"
check_eq "созданы 24 секрета" "${#SECRETS_CREATED[@]}" 24
all_ok=1
for name in "${SECRET_NAMES[@]}"; do
    [ -f "$SECRETS_DIR/$name" ] || all_ok=0
    [ "$(stat -c %a "$SECRETS_DIR/$name")" = 644 ] || all_ok=0
    v=$(cat "$SECRETS_DIR/$name")
    if is_placeholder "$v"; then all_ok=0; fi
done
check "все 26 файлов есть, 0644, без dev-значений" test "$all_ok" = 1
check_eq "max_bot_token пустой" "$(wc -c <"$SECRETS_DIR/max_bot_token" | tr -d ' ')" 0
for name in sms_api_user sms_api_password wg_private_key wg_preshared_key; do
    check_eq "$name создан пустым, не hex" "$(wc -c <"$SECRETS_DIR/$name" | tr -d ' ')" 0
done
check "jwt-подобный: 64 hex" bash -c '[[ $(cat "$1") =~ ^[0-9a-f]{64}$ ]]' _ "$SECRETS_DIR/internal_auth_hmac_key"
check "пароль БД: 32 hex" bash -c '[[ $(cat "$1") =~ ^[0-9a-f]{32}$ ]]' _ "$SECRETS_DIR/postgres_user_password"
check "пароли БД разные" test "$(cat "$SECRETS_DIR/postgres_user_password")" != "$(cat "$SECRETS_DIR/postgres_locker_password")"
snapshot=$(cd "$SECRETS_DIR" && for f in *; do printf '%s=%s\n' "$f" "$(cat "$f")"; done)
SECRET_MAX_BOT_TOKEN=""
ensure_secrets >/dev/null
check_eq "повторный запуск ничего не создаёт" "${#SECRETS_CREATED[@]}" 0
check_eq "повторный запуск ничего не меняет" "$(cd "$SECRETS_DIR" && for f in *; do printf '%s=%s\n' "$f" "$(cat "$f")"; done)" "$snapshot"
SECRET_MAX_BOT_TOKEN=bot-token-1
ensure_secrets >/dev/null
check_eq "пустой max_bot_token заполняется введённым" "$(cat "$SECRETS_DIR/max_bot_token")" bot-token-1
SECRET_MAX_BOT_TOKEN=bot-token-2
ensure_secrets >/dev/null
check_eq "непустой max_bot_token не перезаписывается" "$(cat "$SECRETS_DIR/max_bot_token")" bot-token-1
SECRET_MAX_BOT_TOKEN=""
SECRET_SMS_API_USER=sms-user-1
SECRET_WG_PRIVATE_KEY=wg-key-1
ensure_secrets >/dev/null
check_eq "пустой sms_api_user заполняется введённым" "$(cat "$SECRETS_DIR/sms_api_user")" sms-user-1
check_eq "пустой wg_private_key заполняется введённым" "$(cat "$SECRETS_DIR/wg_private_key")" wg-key-1
check_eq "незаданный sms_api_password остаётся пустым" "$(wc -c <"$SECRETS_DIR/sms_api_password" | tr -d ' ')" 0
SECRET_SMS_API_USER=sms-user-2
ensure_secrets >/dev/null
check_eq "непустой sms_api_user не перезаписывается" "$(cat "$SECRETS_DIR/sms_api_user")" sms-user-1
SECRET_SMS_API_USER=""
SECRET_WG_PRIVATE_KEY=""
SECRET_SMS_API_PASSWORD='pa ss"w'\''d'
ensure_secrets >/dev/null
check_eq "sms_api_password с пробелом и кавычками записан дословно" "$(cat "$SECRETS_DIR/sms_api_password")" 'pa ss"w'\''d'
SECRET_SMS_API_PASSWORD=""

set_root "$TROOT/secroot2"
SECRET_ADMIN_PASSWORD="Typed-Pass-123"
ensure_secrets >/dev/null
check_eq "введённый пароль администратора записан" "$(cat "$SECRETS_DIR/admin_password")" Typed-Pass-123
SECRET_ADMIN_PASSWORD=""
set_root "$TROOT/secroot3"
SECRET_ADMIN_PASSWORD=changeme
rc=0
(ensure_secrets >/dev/null 2>&1) || rc=$?
check "dev-значение пароля отвергается" test "$rc" -ne 0
SECRET_ADMIN_PASSWORD=""

MOCK_BIN=$TROOT/bin
mkdir -p "$MOCK_BIN"
cat >"$MOCK_BIN/openssl" <<'MOCK'
#!/bin/sh
echo changeme
MOCK
chmod +x "$MOCK_BIN/openssl"
v=$(PATH="$MOCK_BIN:$PATH" gen_hex 16)
check "gen_hex: мусор от openssl -> запасной источник" bash -c '[[ $1 =~ ^[0-9a-f]{32}$ ]]' _ "$v"
check "valid_password: changeme отвергается" bash -c '! (source "$1"; valid_password changeme)' _ "$DEPLOY_SH"
check "valid_password: короткий отвергается" bash -c '! (source "$1"; valid_password abc1234)' _ "$DEPLOY_SH"
check "valid_password: нормальный" valid_password 'Str0ng-pass'
printf 'internal-auth-dev-key-do-not-use-in-prod' >"$SECRETS_DIR/internal_auth_hmac_key"
rc=0
audit_secrets production >/dev/null 2>&1 || rc=$?
check "audit: dev-ключ в production -> ошибка" test "$rc" -ne 0
rc=0
audit_secrets development >/dev/null 2>&1 || rc=$?
check "audit: dev-ключ в development -> только предупреждение" test "$rc" -eq 0

# ---------------------------------------------------------------------------
echo "==> Тест 6: публичный адрес"
parse_public_url "http://10.1.2.3"
check_eq "http IP: origin" "$PU_ORIGIN" "http://10.1.2.3"
check_eq "http IP: SECURE_COOKIES" "$PU_SECURE" false
parse_public_url "HTTPS://Scud.Example.com:443/login?x=1"
check_eq "https: origin без пути и порта 443, нижний регистр" "$PU_ORIGIN" "https://scud.example.com"
check_eq "https: SECURE_COOKIES" "$PU_SECURE" true
check_eq "https: хост" "$PU_HOST" scud.example.com
parse_public_url "http://scud.local:8080/"
check_eq "нестандартный порт сохраняется" "$PU_ORIGIN" "http://scud.local:8080"
parse_public_url "http://host:80"
check_eq "порт 80 для http отбрасывается" "$PU_ORIGIN" "http://host"
for bad in "ftp://x" "http://" "scud.example.com" "https://host:99999" "https://host:0" "http://-bad" "http://a b"; do
    check "неверный адрес отвергается: $bad" bash -c '! (source "$1"; parse_public_url "$2")' _ "$DEPLOY_SH" "$bad"
done
check "valid_cidrs" valid_cidrs "127.0.0.1/8 10.0.0.5"
check "valid_cidrs: мусор" bash -c '! (source "$1"; valid_cidrs "10.0.0.1 *")' _ "$DEPLOY_SH"
check "valid_cidrs: пусто" bash -c '! (source "$1"; valid_cidrs "")' _ "$DEPLOY_SH"
check "valid_san" valid_san "scud.example.com 10.0.0.5"
check "valid_phone" valid_phone "+79000000000"
check "valid_phone: без плюса" bash -c '! (source "$1"; valid_phone 79000000000)' _ "$DEPLOY_SH"

# ---------------------------------------------------------------------------
echo "==> Тест 7: разбор аргументов"
parse_args install --dir /x/y -y --ref v1 --no-build
check_eq "команда" "$CMD" install
check_eq "--dir" "$OPT_DIR" /x/y
check_eq "-y" "$ASSUME_YES" 1
check_eq "--ref" "$OPT_REF" v1
check_eq "--no-build" "$NO_BUILD" 1
parse_args --backup-dir=/b update --skip-backup --reconfigure
check_eq "опции до команды и --opt=value" "$CMD:$OPT_BACKUP_DIR:$SKIP_BACKUP:$RECONFIGURE" "update:/b:1:1"
parse_args pack --with-images -o /tmp/r.tar.gz
check_eq "pack" "$CMD:$WITH_IMAGES:$OPT_OUT" "pack:1:/tmp/r.tar.gz"
parse_args
check_eq "без аргументов: меню" "$CMD" ""
for bad in "--bogus" "frobnicate" "install update" "install --reconfigure" "update --with-images" "--dir" "update -o x" "status --baseline" "install --git u --archive a"; do
    rc=0
    # shellcheck disable=SC2086
    (parse_args $bad) 2>/dev/null || rc=$?
    check_eq "ошибка аргументов: $bad" "$rc" 2
done
msg=$(parse_args --bogus 2>&1)
check "сообщение о неизвестной опции" bash -c '[[ $1 == *"неизвестная опция: --bogus"* ]]' _ "$msg"
parse_args

out=$(bash "$DEPLOY_SH" --help)
check_eq "--help: код 0" "$?" 0
check "--help: справка на русском" bash -c '[[ $1 == *"Использование: deploy.sh [команда] [опции]"* ]]' _ "$out"
rc=0
bash "$DEPLOY_SH" --bogus >/dev/null 2>&1 || rc=$?
check_eq "запуск с неизвестной опцией: код 2" "$rc" 2

check "version_ge 2.29.7 >= 2.20" version_ge 2.29.7 2.20
check "version_ge 5.6.1 >= 2.20" version_ge 5.6.1 2.20
check "version_ge 2.20 >= 2.20" version_ge 2.20 2.20
check "version_ge 2.9.0 < 2.20" bash -c '! (source "$1"; version_ge 2.9.0 2.20)' _ "$DEPLOY_SH"
check_eq "norm_image без тега" "$(norm_image backend-acm)" backend-acm:latest
check_eq "rollback_tag" "$(rollback_tag scud-mqtt:dev)" scud-mqtt:rollback
check_eq "profiles_from_services" "$(printf 'user_api\nbackup\nzabbix_web\nmqtt_tls\nzabbix_server\n' | profiles_from_services)" "backup,monitoring,mqtt-tls"
check_eq "profiles_from_services: wg_gateway -> sms" "$(printf 'user_api\nwg_gateway\n' | profiles_from_services)" sms
check "ALL_PROFILES содержит sms" has_profile "$ALL_PROFILES" sms
check_eq "profiles_with: без дублей" "$(profiles_with backup,mqtt-tls backup)" backup,mqtt-tls
check_eq "profiles_with: в пустой" "$(profiles_with "" backup)" backup
check "has_profile" has_profile "backup,mqtt-tls" mqtt-tls
check "has_profile: нет подстрок" bash -c '! (source "$1"; has_profile "mqtt-tls" mqtt)' _ "$DEPLOY_SH"

ASSUME_YES=1
ask APP_ENV "режим" production valid_app_env
check_eq "ask -y: умолчание" "$ANS_APP_ENV" production
SCUD_APP_ENV=development ask APP_ENV "режим" production valid_app_env
check_eq "ask: SCUD_ переменная главнее" "$ANS_APP_ENV" development
rc=0
(SCUD_APP_ENV=bogus ask APP_ENV "режим" production valid_app_env) 2>/dev/null || rc=$?
check "ask: недопустимое SCUD_ -> ошибка" test "$rc" -ne 0
rc=0
(ask PUBLIC_URL "адрес" "" valid_url) 2>/dev/null || rc=$?
check "ask -y без умолчания для обязательного -> ошибка" test "$rc" -ne 0
ask_yn MQTT_TLS "tls" n
check_eq "ask_yn -y: n" "$ANS_MQTT_TLS" n
SCUD_MQTT_TLS=да ask_yn MQTT_TLS "tls" n
check_eq "ask_yn: «да» -> y" "$ANS_MQTT_TLS" y
ASSUME_YES=0
ask APP_ENV "режим" production valid_app_env 2>/dev/null <<<""
check_eq "ask: пустой ввод -> умолчание" "$ANS_APP_ENV" production
ask APP_ENV "режим" production valid_app_env 2>/dev/null <<<$'bogus\ndevelopment'
check_eq "ask: повтор после неверного ввода" "$ANS_APP_ENV" development

# ---------------------------------------------------------------------------
echo "==> Тест 8: MANIFEST и обновление из архива"
set_root "$TROOT/site"
mkdir -p "$ROOT_DIR"
REL1=$TROOT/rel1
mkdir -p "$REL1/backend/scripts" "$REL1/backend/old" "$REL1/frontend" "$REL1/backend/secrets" "$REL1/backend/mqtt-tls/certs"
printf 'SHA=%s\nREF=v1\n' 1111111111111111111111111111111111111111 >"$REL1/VERSION"
echo v1 >"$REL1/backend/docker-compose.yaml"
echo keep >"$REL1/backend/scripts/keep.sh"
echo gone >"$REL1/backend/old/removed.txt"
echo page >"$REL1/frontend/index.html"
: >"$REL1/backend/secrets/.gitkeep"
: >"$REL1/backend/mqtt-tls/certs/.gitkeep"
echo big >"$REL1/images.tar"
release_manifest "$REL1" >"$TROOT/m1"
check "manifest: нет images.tar" bash -c '! grep -qx images.tar "$1"' _ "$TROOT/m1"
check "manifest: нет secrets/ и mqtt-tls/certs/" bash -c '! grep -Eq "secrets|mqtt-tls/certs" "$1"' _ "$TROOT/m1"
check "manifest: есть VERSION" grep -qx VERSION "$TROOT/m1"

CONF_PREV_VERSION=""
install_tree "$REL1" 1111111111111111111111111111111111111111
check "релиз 1: файлы на месте" test -f "$ROOT_DIR/backend/old/removed.txt"
check "релиз 1: images.tar не скопирован" test ! -e "$ROOT_DIR/images.tar"
check "релиз 1: MANIFEST" test -s "$STATE_DIR/MANIFEST"
check "релиз 1: архив для отката" test -s "$STATE_DIR/releases/1111111111111111111111111111111111111111.tar.gz"

mkdir -p "$SECRETS_DIR" "$BACKEND_DIR/mqtt-tls/certs" "$BACKEND_DIR/mqtt-tls/ca"
echo secret >"$SECRETS_DIR/jwt_secret"
echo env >"$ENV_FILE"
echo conf >"$CONF_FILE"
echo pem >"$BACKEND_DIR/mqtt-tls/certs/server.pem"
echo key >"$BACKEND_DIR/mqtt-tls/ca/ca.key"
echo ovr >"$BACKEND_DIR/docker-compose.override.yaml"
echo local >"$BACKEND_DIR/local-notes.txt"
# Подложенные в старый MANIFEST локальные пути и выход за каталог удаляться не должны.
printf 'backend/.env\nbackend/secrets/jwt_secret\nbackend/deploy.conf\nbackend/mqtt-tls/certs/server.pem\n../outside\n' >>"$STATE_DIR/MANIFEST"
echo outside >"$TROOT/outside"

REL2=$TROOT/rel2
mkdir -p "$REL2/backend/scripts" "$REL2/frontend" "$REL2/backend/secrets"
printf 'SHA=%s\nREF=v2\n' 2222222222222222222222222222222222222222 >"$REL2/VERSION"
echo v2 >"$REL2/backend/docker-compose.yaml"
echo keep2 >"$REL2/backend/scripts/keep.sh"
echo new >"$REL2/backend/scripts/new.sh"
echo page >"$REL2/frontend/index.html"
echo attacker >"$REL2/backend/secrets/jwt_secret"
echo attacker >"$REL2/backend/.env"
CONF_PREV_VERSION=1111111111111111111111111111111111111111
install_tree "$REL2" 2222222222222222222222222222222222222222 >"$TROOT/out8" 2>&1
check "релиз 2: удалённый в релизе файл удалён" test ! -e "$ROOT_DIR/backend/old/removed.txt"
check "релиз 2: пустой каталог удалён" test ! -d "$ROOT_DIR/backend/old"
check "релиз 2: сообщение об удалении" grep -q "backend/old/removed.txt" "$TROOT/out8"
check_eq "релиз 2: файл обновлён" "$(cat "$ROOT_DIR/backend/scripts/keep.sh")" keep2
check "релиз 2: новый файл" test -f "$ROOT_DIR/backend/scripts/new.sh"
check_eq "локальный секрет сохранён и не перезаписан" "$(cat "$SECRETS_DIR/jwt_secret")" secret
check_eq ".env сохранён и не перезаписан" "$(cat "$ENV_FILE")" env
check "deploy.conf сохранён" test -f "$CONF_FILE"
check "server.pem сохранён" test -f "$BACKEND_DIR/mqtt-tls/certs/server.pem"
check "ca.key сохранён" test -f "$BACKEND_DIR/mqtt-tls/ca/ca.key"
check "override сохранён" test -f "$BACKEND_DIR/docker-compose.override.yaml"
check "локальный неотслеживаемый файл сохранён" test -f "$BACKEND_DIR/local-notes.txt"
check "путь за пределами каталога не тронут" test -f "$TROOT/outside"
check "MANIFEST обновлён" grep -qx backend/scripts/new.sh "$STATE_DIR/MANIFEST"
check "MANIFEST без старого файла" bash -c '! grep -qx backend/old/removed.txt "$1"' _ "$STATE_DIR/MANIFEST"

: >"$TROOT/empty"
printf 'backend/scripts/new.sh\nbackend/.env\n' >"$TROOT/old"
rc=0
apply_manifest_removals "$ROOT_DIR" "$TROOT/old" "$TROOT/empty" >/dev/null 2>&1 || rc=$?
check "пустой новый MANIFEST: отказ" test "$rc" -ne 0
check "пустой новый MANIFEST: прежние файлы целы" test -e "$ROOT_DIR/backend/scripts/new.sh"
check "пустой новый MANIFEST: .env цел" test -f "$ENV_FILE"
manifest_before=$(cat "$STATE_DIR/MANIFEST")
mkdir -p "$TROOT/emptyrel"
for src in "$TROOT/emptyrel" "$TROOT/no-such-release"; do
    rc=0
    install_tree "$src" 3333333333333333333333333333333333333333 >/dev/null 2>&1 || rc=$?
    check "install_tree: пустой/недоступный релиз $(basename "$src") -> ошибка" test "$rc" -ne 0
    check "install_tree: $(basename "$src") — файлы прежнего релиза целы" test -e "$ROOT_DIR/backend/scripts/new.sh"
    check_eq "install_tree: $(basename "$src") — MANIFEST прежний" "$(cat "$STATE_DIR/MANIFEST")" "$manifest_before"
done

for i in 3 4 5; do
    d=$TROOT/rel$i
    mkdir -p "$d/backend"
    sha=$(printf "%040d" "$i")
    printf 'SHA=%s\n' "$sha" >"$d/VERSION"
    echo "v$i" >"$d/backend/docker-compose.yaml"
    sleep 1
    CONF_PREV_VERSION=1111111111111111111111111111111111111111
    install_tree "$d" "$sha" >/dev/null 2>&1
done
check_eq "хранятся 3 последних релиза + предыдущий" "$(find "$STATE_DIR/releases" -name '*.tar.gz' | wc -l | tr -d ' ')" 4
check "релиз PREV_VERSION не удалён" test -f "$STATE_DIR/releases/1111111111111111111111111111111111111111.tar.gz"

check "release_info: корректный VERSION" release_info "$REL2"
printf 'SHA=xyz\n' >"$TROOT/rel3/VERSION"
check "release_info: битый SHA отвергается" bash -c '! (source "$1"; release_info "$2")' _ "$DEPLOY_SH" "$TROOT/rel3"
check "is_protected_path backend/.env" is_protected_path backend/.env
check "is_protected_path обычный файл" bash -c '! (source "$1"; is_protected_path backend/scripts/deploy.sh)' _ "$DEPLOY_SH"

# ---------------------------------------------------------------------------
echo "==> Тест 8a: образы для отката, сертификаты MQTT, профили, git без сети"
set_root "$TROOT/tagroot"
rc=0
(
    local_built_images() { return 1; }
    tag_rollback_images
) >/dev/null 2>&1 || rc=$?
check "tag_rollback_images: сбой config --images -> ошибка" test "$rc" -ne 0
rc=0
(
    local_built_images() { :; }
    tag_rollback_images
) >/dev/null 2>&1 || rc=$?
check "tag_rollback_images: пустой список образов -> ошибка" test "$rc" -ne 0
check "tag_rollback_images: список для отката не создан" test ! -s "$STATE_DIR/rollback-images"

set_root "$TROOT/tls"
mkdir -p "$BACKEND_DIR/scripts"
GEN_LOG=$TROOT/tls/gen.log
export GEN_LOG
printf '#!/bin/sh\nprintf "%%s\\n" "$*" >>"$GEN_LOG"\n' >"$BACKEND_DIR/scripts/gen-mqtt-tls-certs.sh"
: >"$ENV_FILE"
tls_run() {
    : >"$GEN_LOG"
    (
        CONF_PROFILES=$1
        CONF_MQTT_TLS_SAN="scud.example.com 10.0.0.5"
        ensure_tls_certs
    ) >"$TROOT/tls/out" 2>&1
}
tls_run backup
check "TLS: профиль выключен — генератор не вызывается" test ! -s "$GEN_LOG"
tls_run mqtt-tls
check_eq "TLS: первая установка — генератор с --out backend/mqtt-tls" "$(cat "$GEN_LOG")" "--out $BACKEND_DIR/mqtt-tls --dns scud.example.com --ip 10.0.0.5"
mkdir -p "$BACKEND_DIR/mqtt-tls/certs"
echo pem >"$BACKEND_DIR/mqtt-tls/certs/server.pem"
tls_run mqtt-tls
check "TLS: server.pem есть — генератор не вызывается" test ! -s "$GEN_LOG"
check "TLS: сообщение о готовом сертификате" grep -q "уже есть" "$TROOT/tls/out"
kv_set "$ENV_FILE" MQTT_TLS_CERT_DIR "$TROOT/tls/custom/certs"
tls_run mqtt-tls
check_eq "TLS: свой MQTT_TLS_CERT_DIR/certs — --out его родитель" "$(cut -d' ' -f1-2 "$GEN_LOG")" "--out $TROOT/tls/custom"
mkdir -p "$TROOT/tls/custom/certs"
echo pem >"$TROOT/tls/custom/certs/server.pem"
tls_run mqtt-tls
check "TLS: свой каталог с server.pem — генератор не вызывается" test ! -s "$GEN_LOG"
kv_set "$ENV_FILE" MQTT_TLS_CERT_DIR ./tlsdir
tls_run mqtt-tls
check_eq "TLS: каталог не certs — выпуск в backend/mqtt-tls" "$(cut -d' ' -f1-2 "$GEN_LOG")" "--out $BACKEND_DIR/mqtt-tls"
check "TLS: каталог не certs — оператору сказано скопировать" grep -q "скопируйте его в $BACKEND_DIR/tlsdir" "$TROOT/tls/out"

set_root "$TROOT/resume"
mkdir -p "$SECRETS_DIR"
printf 'Adm1n-pass-x' >"$SECRETS_DIR/admin_password"
daily_default() {
    (
        ASSUME_YES=1
        CONF_STATE=$1
        CONF_PROFILES=$2
        CONF_PUBLIC_URL=https://scud.example.com
        ask_config install >/dev/null 2>&1
        printf '%s' "$ANS_BACKUP_DAILY"
    )
}
check_eq "install: ежедневные дампы по умолчанию включены" "$(daily_default "" "")" y
check_eq "продолжение install: отказ от дампов сохраняется" "$(daily_default installing mqtt-tls)" n
check_eq "продолжение install: включённые дампы сохраняются" "$(daily_default installing backup)" y
sms_answer() {
    (
        ASSUME_YES=1
        CONF_STATE=installing
        CONF_PROFILES=$1
        CONF_PUBLIC_URL=https://scud.example.com
        ask_config install >/dev/null 2>&1
        printf '%s' "$ANS_SMS"
    )
}
check_eq "sms: по умолчанию выключен" "$(sms_answer "")" n
check_eq "sms: включённый профиль сохраняется по умолчанию" "$(sms_answer backup,sms)" y
check_eq "sms: SCUD_SMS=y включает" "$(SCUD_SMS=y sms_answer "")" y
check_eq "sms: SCUD_SMS=n выключает" "$(SCUD_SMS=n sms_answer sms)" n
check_eq "sms: SCUD_SMS_API_USER/PASSWORD попадают в секреты" "$(
    SCUD_SMS=y SCUD_SMS_API_USER=u1 SCUD_SMS_API_PASSWORD=p1 bash -c '
        source "$1"
        parse_args
        set_root "$2"
        mkdir -p "$SECRETS_DIR"
        ASSUME_YES=1 CONF_STATE=installing CONF_PROFILES="" CONF_PUBLIC_URL=https://scud.example.com
        ask_config install >/dev/null 2>&1
        printf "%s/%s" "$SECRET_SMS_API_USER" "$SECRET_SMS_API_PASSWORD"' _ "$DEPLOY_SH" "$TROOT/smsask"
)" u1/p1
check_eq "ask_line: пароль с пробелом и кавычкой принят дословно" "$(
    SCUD_SMS_API_PASSWORD='a b"c' bash -c 'source "$1"; parse_args; ask_line SMS_API_PASSWORD x 1; printf "%s" "$ANS_SMS_API_PASSWORD"' _ "$DEPLOY_SH"
)" 'a b"c'
rc=0
out=$(SCUD_SMS_API_PASSWORD=$'SECRETMARK\nline2' bash -c 'source "$1"; parse_args; ask_line SMS_API_PASSWORD x 1' _ "$DEPLOY_SH" 2>&1) || rc=$?
check "ask_line: многострочный пароль отклоняется" test "$rc" -ne 0
check "ask_line: значение пароля не попадает в вывод" bash -c '[[ $1 != *SECRETMARK* && $1 == *SCUD_SMS_API_PASSWORD* ]]' _ "$out"

check_eq "profiles_without: убирает профиль" "$(profiles_without backup,sms,mqtt-tls sms)" backup,mqtt-tls
check_eq "profiles_without: единственный профиль" "$(profiles_without sms sms)" ""
check_eq "profiles_without: пусто" "$(profiles_without "" sms)" ""

set_root "$TROOT/gate"
mkdir -p "$SECRETS_DIR"
for name in sms_api_user sms_api_password wg_private_key wg_preshared_key; do
    : >"$SECRETS_DIR/$name"
done
printf 'TOPSECRETVAL' >"$SECRETS_DIR/sms_api_password"
CONF_PROFILES=backup,sms
SMS_DEFERRED=0
gate_sms_profile >"$TROOT/gate.out" 2>&1
check_eq "gate: нет значений — sms снят" "$CONF_PROFILES" backup
check "gate: перечислены все недостающие имена" grep -q "secrets/sms_api_user secrets/wg_private_key secrets/wg_preshared_key .env:WG_PEER_PUBKEY .env:SMS_GATEWAY_URL" "$TROOT/gate.out"
check "gate: заполненный секрет не перечислен, значение не выводится" bash -c '! grep -q "sms_api_password\|TOPSECRETVAL" "$1"' _ "$TROOT/gate.out"
check "gate: подсказка про update --reconfigure" grep -q "update --reconfigure" "$TROOT/gate.out"
check_eq "gate: без defer SMS_DEFERRED не ставится" "$SMS_DEFERRED" 0
CONF_PROFILES=backup
gate_sms_profile >"$TROOT/gate.out" 2>&1
check_eq "gate: без sms в профилях тишина" "$(wc -c <"$TROOT/gate.out" | tr -d ' ')" 0
printf 'u' >"$SECRETS_DIR/sms_api_user"
printf 'k' >"$SECRETS_DIR/wg_private_key"
printf 'k' >"$SECRETS_DIR/wg_preshared_key"
printf 'WG_PEER_PUBKEY=abc\nSMS_GATEWAY_URL=\n' >"$ENV_FILE"
CONF_PROFILES=sms
gate_sms_profile defer >"$TROOT/gate.out" 2>&1
check_eq "gate: пустой SMS_GATEWAY_URL — sms снят" "$CONF_PROFILES" ""
check_eq "gate: defer помечает снятие" "$SMS_DEFERRED" 1
check "gate: только SMS_GATEWAY_URL в перечне" grep -q "не заполнено .env:SMS_GATEWAY_URL " "$TROOT/gate.out"
SMS_DEFERRED=0
printf 'WG_PEER_PUBKEY=abc\nSMS_GATEWAY_URL=http://wg_gateway:7777\n' >"$ENV_FILE"
CONF_PROFILES=backup,sms
gate_sms_profile defer >"$TROOT/gate.out" 2>&1
check_eq "gate: всё заполнено — sms остаётся" "$CONF_PROFILES" backup,sms
check_eq "gate: всё заполнено — defer не ставится" "$SMS_DEFERRED" 0
CONF_PROFILES=""

DCP_LOG=$TROOT/dcp.log
: >"$DCP_LOG"
(
    dc_p() {
        local profs=$1
        shift
        printf '%s|%s\n' "$profs" "$*" >>"$DCP_LOG"
        case "$*" in
            "config --services")
                if [ "$profs" = "$ALL_PROFILES" ]; then
                    printf 'user_api\nbackup\nzabbix_web\nmqtt_tls\n'
                else
                    printf 'user_api\nmqtt_tls\n'
                fi
                ;;
            "ps -a --services") printf 'user_api\nbackup\nmqtt_tls\n' ;;
        esac
    }
    CONF_PROFILES=mqtt-tls
    stop_disabled_profile_services
) >/dev/null 2>&1
check "выключенный профиль: сервис остановлен" grep -qx "$ALL_PROFILES|stop backup" "$DCP_LOG"
check "выключенный профиль: контейнер удалён" grep -qx "$ALL_PROFILES|rm -f backup" "$DCP_LOG"
check "выключенный профиль: сервисы без контейнеров и включённые не трогаются" bash -c '! grep -Eq "(stop|rm -f) .*(zabbix_web|mqtt_tls|user_api)" "$1"' _ "$DCP_LOG"

if command -v git >/dev/null 2>&1; then
    GA=$TROOT/git-origin
    GB=$TROOT/git-clone
    git init -q "$GA"
    git -C "$GA" checkout -q -b main 2>/dev/null || true
    echo a >"$GA/f.txt"
    git -C "$GA" add -A
    git -C "$GA" -c user.email=t@example.org -c user.name=t commit -q -m a
    git clone -q "$GA" "$GB"
    git -C "$GB" remote set-url origin "$TROOT/no-such-remote"
    set_root "$GB"
    CONF_SOURCE=git
    rc=0
    (git_update_code main) >"$TROOT/git.out" 2>&1 || rc=$?
    check_eq "git fetch не удался, ревизия есть локально: продолжение" "$rc" 0
    check "git fetch не удался: предупреждение" grep -q "уже есть в локальном репозитории" "$TROOT/git.out"
    check_eq "git fetch не удался: checkout выполнен" "$(git -C "$GB" rev-parse HEAD)" "$(git -C "$GA" rev-parse HEAD)"
    rc=0
    (git_update_code no-such-ref) >"$TROOT/git2.out" 2>&1 || rc=$?
    check "git fetch не удался, ревизии нет: ошибка" test "$rc" -ne 0
    check "git fetch не удался, ревизии нет: сообщение" grep -q "локально нет" "$TROOT/git2.out"
    CONF_SOURCE=""
else
    err_t "git не установлен — тест git_update_code пропущен"
fi

# ---------------------------------------------------------------------------
echo "==> Тест 9: pack"
if command -v git >/dev/null 2>&1; then
    G=$TROOT/gitrepo
    mkdir -p "$G/backend/scripts"
    git init -q "$G"
    git -C "$G" checkout -q -b rel/main 2>/dev/null || true
    echo compose >"$G/backend/docker-compose.yaml"
    cp "$DEPLOY_SH" "$G/backend/scripts/deploy.sh"
    echo one >"$G/tracked.txt"
    git -C "$G" add -A
    git -C "$G" -c user.email=t@example.org -c user.name=t commit -q -m one
    first=$(git -C "$G" rev-parse HEAD)
    echo two >"$G/tracked.txt"
    git -C "$G" -c user.email=t@example.org -c user.name=t commit -q -am two
    sha=$(git -C "$G" rev-parse HEAD)

    rc=0
    (pack_release "$G" no-such-ref "$TROOT/x.tar.gz" 0) >/dev/null 2>&1 || rc=$?
    check "pack: несуществующая ревизия -> ошибка" test "$rc" -ne 0
    check "pack: архив не создан" test ! -e "$TROOT/x.tar.gz"

    echo dirty >"$G/tracked.txt"
    rc=0
    msg=$( (pack_release "$G" HEAD "$TROOT/x.tar.gz" 0) 2>&1) || rc=$?
    check "pack: грязная рабочая копия -> ошибка" test "$rc" -ne 0
    check "pack: сообщение о незакоммиченных изменениях" bash -c '[[ $1 == *"незакоммиченные"* ]]' _ "$msg"
    rc=0
    (pack_release "$G" "$first" "$TROOT/old.tar.gz" 0) >/dev/null 2>&1 || rc=$?
    check "pack: старая ревизия при грязной копии допускается" test "$rc" -eq 0
    git -C "$G" checkout -q -- tracked.txt

    echo untracked >"$G/untracked.txt"
    echo secret >"$G/backend/.env"
    rc=0
    (cd "$TROOT" && pack_release "$G" HEAD "" 0) >"$TROOT/pack.out" 2>&1 || rc=$?
    check_eq "pack: успех" "$rc" 0
    arch=$(find "$TROOT" -maxdepth 1 -name "scud-*-${sha:0:8}.tar.gz" | head -n 1)
    check "pack: имя scud-<ref>-<sha>.tar.gz" test -n "$arch"
    if [ -n "$arch" ]; then
        listing=$(tar -tzf "$arch")
        top=$(printf '%s\n' "$listing" | head -n 1 | cut -d/ -f1)
        check "pack: VERSION" bash -c '[[ $1 == *"/VERSION"* ]]' _ "$listing"
        check "pack: deploy.sh в корне" bash -c '[[ $1 == *"$2/deploy.sh"* ]]' _ "$listing" "$top"
        check "pack: нет неотслеживаемых файлов" bash -c '[[ $1 != *untracked.txt* ]] && [[ $1 != *backend/.env* ]]' _ "$listing"
        check "pack: нет pax_global_header" bash -c '[[ $1 != *pax_global_header* ]]' _ "$listing"
        X=$TROOT/unpacked
        mkdir -p "$X"
        tar -xzf "$arch" -C "$X"
        check "pack: VERSION читается release_info" release_info "$X/$top"
        check_eq "pack: SHA в VERSION" "$REL_SHA" "$sha"
        check_eq "pack: содержимое ревизии" "$(cat "$X/$top/tracked.txt")" two
    fi
    rc=0
    (pack_release "$TROOT/inst" HEAD "$TROOT/y.tar.gz" 0) >/dev/null 2>&1 || rc=$?
    check "pack: не git-каталог -> ошибка" test "$rc" -ne 0
else
    err_t "git не установлен — тест pack пропущен (apk add git)"
fi

# ---------------------------------------------------------------------------
echo "==> Тест 10: status без docker"
cat >"$MOCK_BIN/docker" <<'MOCK'
#!/bin/sh
exit 1
MOCK
chmod +x "$MOCK_BIN/docker"
EMPTY_DIR=$TROOT/nothing
mkdir -p "$EMPTY_DIR"
out=$(PATH="$MOCK_BIN:$PATH" bash "$DEPLOY_SH" status --dir "$EMPTY_DIR" 2>&1)
rc=$?
check_eq "status пустого каталога: код 0" "$rc" 0
check "status пустого каталога: «не найдена»" bash -c '[[ $1 == *"не найдена"* ]]' _ "$out"

S=$TROOT/statusroot
mkdir -p "$S/backend"
make_tree "$S/backend"
echo compose >"$S/backend/docker-compose.yaml"
printf 'SHA=%s\nREF=v9\n' 9999999999999999999999999999999999999999 >"$S/VERSION"
cat >"$S/backend/deploy.conf" <<'CONF'
SOURCE=archive
GIT_REF=v9
PROFILES=backup
BACKUP_DIR=/nonexistent/backups
PUBLIC_URL=https://scud.example.com
VERSION=9999999999999999999999999999999999999999
STATE=installed
CONF
out=$(PATH="$MOCK_BIN:$PATH" bash "$DEPLOY_SH" status --dir "$S" 2>&1)
rc=$?
check_eq "status установки без docker: код 0" "$rc" 0
check "status: версия" bash -c '[[ $1 == *"v9 (99999999)"* ]]' _ "$out"
check "status: блоки из файлов" bash -c '[[ $1 == *"scud-lockers-one extra"* ]]' _ "$out"
check "status: предупреждение про docker" bash -c '[[ $1 == *"Docker недоступен"* ]]' _ "$out"

# ---------------------------------------------------------------------------
echo "==> Тест 11: install -> update -> rollback -> reinstall с моком docker"
E2E=$TROOT/e2e
EBIN=$E2E/bin
MOCK_STATE=$E2E/state
mkdir -p "$EBIN" "$MOCK_STATE"
export MOCK_STATE
# Мок docker: журнал схемы в $MOCK_STATE/journal.<db>, все вызовы — в $MOCK_STATE/log.
cat >"$EBIN/docker" <<'MOCK'
#!/bin/sh
st=$MOCK_STATE
printf '%s\n' "docker $*" >>"$st/log"
case "$1" in
    info | tag | load | pull | save | image | ps) exit 0 ;;
    volume) cat "$st/volumes" 2>/dev/null; exit 0 ;;
    compose) shift ;;
    *) exit 1 ;;
esac
while [ "$#" -gt 0 ]; do
    case "$1" in
        -p | --profile) shift 2 ;;
        *) break ;;
    esac
done
sub=$1
shift
case "$sub" in
    version) echo 2.29.1 ;;
    config)
        case " $* " in
            *" --services "*) printf 'user_api\nacm\nwg_gateway\n' ;;
            *) printf 'backend-acm\nscud-mqtt:dev\nregistry.example.org/pg:15\n' ;;
        esac
        ;;
    ps)
        case " $* " in
            *" wg_gateway "*) [ -f "$st/wg_unhealthy" ] || echo healthy ;;
        esac
        ;;
    up)
        if [ -f "$st/fail_up" ]; then
            case " $* " in
                *" $(cat "$st/fail_up") "*) exit 1 ;;
            esac
        fi
        echo backend_postgres_user_data >"$st/volumes"
        ;;
    down) rm -f "$st/volumes" ;;
    run)
        host=""
        ep=""
        while [ "$#" -gt 0 ]; do
            case "$1" in
                -v) host=${2%%:*}; shift 2 ;;
                --entrypoint) ep=$2; shift 2 ;;
                --rm | --no-deps | -T) shift ;;
                *) break ;;
            esac
        done
        if [ "$ep" = id ]; then
            echo 100
            exit 0
        fi
        if [ -n "$host" ]; then
            for db in scud_users scud_lockers scud_history scud_auth scud_notifications scud_reports; do
                echo dump >"$host/$db-20261106-000000.dump"
            done
        fi
        ;;
    exec)
        while [ "$#" -gt 0 ]; do
            case "$1" in
                -T) shift ;;
                -e) shift 2 ;;
                *) break ;;
            esac
        done
        svc=$1
        cmd=$2
        shift 2
        if [ "$cmd" = bash ]; then
            cat >"$st/exec_stdin.$svc"
            exit 0
        fi
        [ "$cmd" = psql ] || exit 0
        db=""
        n=""
        c=""
        while [ "$#" -gt 0 ]; do
            case "$1" in
                -d) db=$2; shift 2 ;;
                -v)
                    case "$2" in
                        n=*) n=${2#n=} ;;
                        c=*) c=${2#c=} ;;
                    esac
                    shift 2
                    ;;
                *) shift ;;
            esac
        done
        sql=$(cat)
        j=$st/journal.$db
        touch "$j"
        case "$sql" in
            *"SELECT name ||"*) cat "$j" ;;
            *"INSERT INTO deploy_schema_upgrades"*)
                grep -v "^$n " "$j" >"$j.t"
                echo "$n $c" >>"$j.t"
                mv "$j.t" "$j"
                ;;
            *"CREATE TABLE IF NOT EXISTS deploy_schema_upgrades"*) ;;
            *"count(*)"*) echo 0 ;;
            *"SELECT 1 FROM"*) echo 1 ;;
            *) echo "block $db $(printf '%s\n' "$sql" | head -n 1)" >>"$st/log" ;;
        esac
        ;;
esac
exit 0
MOCK
cat >"$EBIN/curl" <<'MOCK'
#!/bin/sh
case "$*" in
    *--data-binary*) cat >/dev/null ;;
esac
printf 200
MOCK
chmod +x "$EBIN/docker" "$EBIN/curl"

make_release() {
    local dir=$1 sha=$2 tag=$3 top
    top=$dir/scud-$tag
    mkdir -p "$top/backend/scripts" "$top/backend/init-scripts" "$top/frontend/public"
    make_tree "$top/backend"
    printf 'REF=%s\nSHA=%s\nDATE=2026-11-06T00:00:00Z\n' "$tag" "$sha" >"$top/VERSION"
    echo "services: {}" >"$top/backend/docker-compose.yaml"
    cp "$DEPLOY_SH" "$top/backend/scripts/deploy.sh"
    cp "$REPO_BACKEND/init-scripts/init-user-db.sh" "$top/backend/init-scripts/init-user-db.sh"
    echo page >"$top/frontend/public/index.html"
    tar -C "$dir" -czf "$dir/scud-$tag.tar.gz" "scud-$tag"
}
SHA1=aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
SHA2=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
mkdir -p "$E2E/r1" "$E2E/r2"
make_release "$E2E/r1" "$SHA1" v1 >/dev/null
echo only-v1 >"$E2E/r1/scud-v1/backend/only-in-v1.txt"
tar -C "$E2E/r1" -czf "$E2E/r1/scud-v1.tar.gz" scud-v1
make_release "$E2E/r2" "$SHA2" v2 >/dev/null
echo "# v2" >>"$E2E/r2/scud-v2/backend/scripts/deploy.sh"
NOTIF=$E2E/r2/scud-v2/backend/services/notification_api/app/notification_data/db/migrations/001_init.sql
printf -- '-- v2-block:begin\nALTER TABLE x ADD COLUMN IF NOT EXISTS y int;\n-- v2-block:end\n' >>"$NOTIF"
tar -C "$E2E/r2" -czf "$E2E/r2/scud-v2.tar.gz" scud-v2

D=$E2E/opt/scud
BK=$E2E/backups
run_deploy() {
    PATH="$EBIN:$PATH" SCUD_PUBLIC_URL=https://scud.example.com bash "$DEPLOY_SH" "$@" --dir "$D" -y </dev/null
}

run_deploy install --archive "$E2E/r1/scud-v1.tar.gz" --backup-dir "$BK" >"$E2E/install.out" 2>&1
rc=$?
check_eq "install: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/install.out" >&2
check_eq "install: STATE" "$(kv_get "$D/backend/deploy.conf" STATE)" installed
check_eq "install: VERSION" "$(kv_get "$D/backend/deploy.conf" VERSION)" "$SHA1"
check_eq "install: профиль backup" "$(kv_get "$D/backend/deploy.conf" PROFILES)" backup
check_eq "install: CORS" "$(kv_get "$D/backend/.env" CORS_ALLOWED_ORIGIN)" https://scud.example.com
check_eq "install: SECURE_COOKIES" "$(kv_get "$D/backend/.env" SECURE_COOKIES)" true
check_eq "install: APP_ENV" "$(kv_get "$D/backend/.env" APP_ENV)" production
check_eq "install: FRONTEND_PATH" "$(kv_get "$D/backend/.env" FRONTEND_PATH)" "$D/frontend"
check_eq "install: REDIS_PASSWORD = секрет" "$(kv_get "$D/backend/.env" REDIS_PASSWORD)" "$(cat "$D/backend/secrets/redis_password")"
check_eq "install: 26 секретов" "$(find "$D/backend/secrets" -type f | wc -l | tr -d ' ')" 26
for name in sms_api_user sms_api_password wg_private_key wg_preshared_key; do
    check_eq "install: $name создан пустым" "$(wc -c <"$D/backend/secrets/$name" | tr -d ' ')" 0
    check_eq "install: $name права 0644" "$(stat -c %a "$D/backend/secrets/$name")" 644
done
check "install: профиль sms по умолчанию выключен" bash -c '! grep -q "^PROFILES=.*sms" "$1"' _ "$D/backend/deploy.conf"
check "install: пароль администратора напечатан" grep -q "$(cat "$D/backend/secrets/admin_password")" "$E2E/install.out"
check "install: блоки не выполнялись (baseline)" bash -c '! grep -q "^block " "$1"' _ "$MOCK_STATE/log"
check "install: блок locker записан в журнал" grep -q "^extra " "$MOCK_STATE/journal.scud_lockers"
check "install: up --wait" grep -q "compose .*up -d --no-build --wait --wait-timeout 600 --remove-orphans" "$MOCK_STATE/log"
check "install: временные каталоги удалены" test -z "$(ls -A "$D/.deploy/tmp" 2>/dev/null)"
check "install: images.tar не в каталоге установки" test ! -e "$D/images.tar"

rc=0
run_deploy install --archive "$E2E/r1/scud-v1.tar.gz" >"$E2E/install2.out" 2>&1 || rc=$?
check "повторный install отказывает" test "$rc" -ne 0
check "повторный install: подсказка" grep -q "используйте update или reinstall" "$E2E/install2.out"

secrets_before=$(cat "$D/backend/secrets/jwt_secret")
echo "LOCAL_TUNING=1" >>"$D/backend/.env"
printf 'sms-operator' >"$D/backend/secrets/sms_api_user"
printf 'wg-priv' >"$D/backend/secrets/wg_private_key"
: >"$MOCK_STATE/log"
run_deploy update --archive "$E2E/r2/scud-v2.tar.gz" >"$E2E/update.out" 2>&1
rc=$?
check_eq "update: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/update.out" >&2
check "update: продолжено новой версией скрипта" grep -q "Продолжение обновления новой версией" "$E2E/update.out"
check_eq "update: VERSION" "$(kv_get "$D/backend/deploy.conf" VERSION)" "$SHA2"
check_eq "update: PREV_VERSION" "$(kv_get "$D/backend/deploy.conf" PREV_VERSION)" "$SHA1"
check "update: файл, удалённый в v2, удалён" test ! -e "$D/backend/only-in-v1.txt"
check_eq "update: секреты не тронуты" "$(cat "$D/backend/secrets/jwt_secret")" "$secrets_before"
check "update: локальная настройка .env сохранена" grep -q '^LOCAL_TUNING=1$' "$D/backend/.env"
check_eq "update: заполненный sms_api_user сохранён" "$(cat "$D/backend/secrets/sms_api_user")" sms-operator
check_eq "update: заполненный wg_private_key сохранён" "$(cat "$D/backend/secrets/wg_private_key")" wg-priv
check_eq "update: пустой sms_api_password остался пустым" "$(wc -c <"$D/backend/secrets/sms_api_password" | tr -d ' ')" 0
check_eq "update: 26 секретов" "$(find "$D/backend/secrets" -type f | wc -l | tr -d ' ')" 26
dump=$(find "$BK" -maxdepth 1 -name '*-pre-update' | head -n 1)
check "update: каталог дампа" test -n "$dump"
check_eq "update: 6 дампов" "$(find "$dump" -name '*.dump' | wc -l | tr -d ' ')" 6
check_eq "update: копия конфигурации 0600" "$(stat -c %a "$dump/config.tar.gz" 2>/dev/null)" 600
check "update: в копии есть .env и секреты" bash -c 'tar -tzf "$1" | grep -q "^\(\./\)\?\.env$" && tar -tzf "$1" | grep -q "secrets/jwt_secret"' _ "$dump/config.tar.gz"
check_eq "update: образы помечены :rollback" "$(wc -l <"$D/.deploy/rollback-images" | tr -d ' ')" 2
check "update: новый блок выполнен" grep -q "^block scud_notifications -- v2-block:begin" "$MOCK_STATE/log"
check "update: новый блок записан" grep -q "^v2-block " "$MOCK_STATE/journal.scud_notifications"
check_eq "update: старые блоки не выполнялись" "$(grep -c '^block ' "$MOCK_STATE/log")" 1
ln_stop=$(grep -n "compose .*stop apache_gateway user_api locker_api" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
ln_block=$(grep -n "^block " "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
ln_hist=$(grep -n " up -d --no-build --wait --wait-timeout 600 history_api$" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
ln_all=$(grep -n " up -d --no-build --wait --wait-timeout 600 --remove-orphans$" "$MOCK_STATE/log" | tail -n 1 | cut -d: -f1)
ln_build=$(grep -n "compose .* build$" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
ln_dump=$(grep -n "compose .*run .*-v .*backup /scripts/backup.sh" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
check "update: порядок дамп < сборка < стоп API < блоки < history_api < все" \
    test "${ln_dump:-0}" -gt 0 -a "${ln_dump:-0}" -lt "${ln_build:-0}" -a "${ln_build:-0}" -lt "${ln_stop:-0}" -a "${ln_stop:-0}" -lt "${ln_block:-0}" -a "${ln_block:-0}" -lt "${ln_hist:-0}" -a "${ln_hist:-0}" -lt "${ln_all:-0}"
check "update: синхронизация acm_ro" grep -q "exec -T -e SKIP_MIGRATIONS=1 postgres_user bash -s$" "$MOCK_STATE/log"
check "update: init-user-db.sh передан в контейнер из файла хоста через stdin" cmp -s "$MOCK_STATE/exec_stdin.postgres_user" "$D/backend/init-scripts/init-user-db.sh"
ln_restart=$(grep -n "compose .* restart postgres_user$" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
ln_wait_pg=$(grep -n " up -d --no-build --wait --wait-timeout 300 postgres_user$" "$MOCK_STATE/log" | tail -n 1 | cut -d: -f1)
ln_acm=$(grep -n "exec -T -e SKIP_MIGRATIONS=1 postgres_user bash" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
check "update: стоп API < перезапуск postgres_user < ожидание healthy < блоки < acm_ro" \
    test "${ln_stop:-0}" -gt 0 -a "${ln_stop:-0}" -lt "${ln_restart:-0}" -a "${ln_restart:-0}" -lt "${ln_wait_pg:-0}" -a "${ln_wait_pg:-0}" -lt "${ln_block:-0}" -a "${ln_block:-0}" -lt "${ln_acm:-0}"
check_eq "update: STATE после успеха" "$(kv_get "$D/backend/deploy.conf" STATE)" installed
check_eq "update: TARGET_VERSION очищен" "$(kv_get "$D/backend/deploy.conf" TARGET_VERSION)" ""

: >"$MOCK_STATE/log"
run_deploy rollback >"$E2E/rollback.out" 2>&1
rc=$?
check_eq "rollback: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/rollback.out" >&2
check_eq "rollback: VERSION" "$(kv_get "$D/backend/deploy.conf" VERSION)" "$SHA1"
check_eq "rollback: PREV_VERSION очищен" "$(kv_get "$D/backend/deploy.conf" PREV_VERSION)" ""
check_eq "rollback: код v1" "$(kv_get "$D/VERSION" SHA)" "$SHA1"
check "rollback: файл v1 вернулся" test -f "$D/backend/only-in-v1.txt"
check "rollback: образ возвращён" grep -q "^docker tag backend-acm:rollback backend-acm:latest$" "$MOCK_STATE/log"
check "rollback: схема не трогалась" bash -c '! grep -q "psql" "$1"' _ "$MOCK_STATE/log"
rc=0
run_deploy rollback >/dev/null 2>&1 || rc=$?
check "повторный rollback отказывает" test "$rc" -ne 0

: >"$MOCK_STATE/log"
echo history_api >"$MOCK_STATE/fail_up"
rc=0
run_deploy update --archive "$E2E/r2/scud-v2.tar.gz" >"$E2E/update_fail.out" 2>&1 || rc=$?
rm -f "$MOCK_STATE/fail_up"
check "update со сбоем после сборки: ненулевой код" test "$rc" -ne 0
check "update со сбоем: подсказка повторить или откатить" grep -q "Обновление прервано после остановки сервисов" "$E2E/update_fail.out"
check_eq "update со сбоем: STATE=updating" "$(kv_get "$D/backend/deploy.conf" STATE)" updating
check_eq "update со сбоем: TARGET_VERSION" "$(kv_get "$D/backend/deploy.conf" TARGET_VERSION)" "$SHA2"
check_eq "update со сбоем: PREV_VERSION" "$(kv_get "$D/backend/deploy.conf" PREV_VERSION)" "$SHA1"
check "update со сбоем: образы помечены :rollback до сборки" grep -q "^docker tag backend-acm:latest backend-acm:rollback$" "$MOCK_STATE/log"
rb_before=$(cat "$D/.deploy/rollback-images")
out=$(PATH="$EBIN:$PATH" bash "$DEPLOY_SH" status --dir "$D" 2>&1)
check "status: незавершённое обновление" bash -c '[[ $1 == *"незавершённое обновление на ${2:0:12}"* ]]' _ "$out" "$SHA2"

: >"$MOCK_STATE/log"
run_deploy update --archive "$E2E/r2/scud-v2.tar.gz" >"$E2E/update2.out" 2>&1
check_eq "повторный update: код 0" "$?" 0
check "повторный update: метки :rollback не перезаписаны" bash -c '! grep -q "^docker tag .*:rollback$" "$1"' _ "$MOCK_STATE/log"
check "повторный update: сообщение о сохранённых метках" grep -q "образы :rollback и версия для отката" "$E2E/update2.out"
check_eq "повторный update: список образов для отката прежний" "$(cat "$D/.deploy/rollback-images")" "$rb_before"
check_eq "повторный update: PREV_VERSION прежний" "$(kv_get "$D/backend/deploy.conf" PREV_VERSION)" "$SHA1"
check_eq "повторный update: STATE" "$(kv_get "$D/backend/deploy.conf" STATE)" installed
check_eq "повторный update: блоки не выполнялись" "$(grep -c '^block ' "$MOCK_STATE/log")" 0
check "повторный update: «уже применён»" grep -q "= v2-block: уже применён" "$E2E/update2.out"

mv "$D/.deploy/rollback-images" "$E2E/rollback-images.saved"
rc=0
run_deploy rollback >"$E2E/rollback_noimg.out" 2>&1 || rc=$?
mv "$E2E/rollback-images.saved" "$D/.deploy/rollback-images"
check "rollback без списка образов :rollback отказывает" test "$rc" -ne 0
check_eq "rollback без образов: версия не изменилась" "$(kv_get "$D/backend/deploy.conf" VERSION)" "$SHA2"

: >"$MOCK_STATE/log"
rc=0
SCUD_CONFIRM_WIPE=УДАЛИТЬ run_deploy reinstall --archive "$E2E/no-such.tar.gz" >"$E2E/reinstall_bad.out" 2>&1 || rc=$?
check "reinstall с недоступным архивом: ошибка" test "$rc" -ne 0
check "reinstall с недоступным архивом: тома не удалялись" bash -c '! grep -q " down " "$1"' _ "$MOCK_STATE/log"
check_eq "reinstall с недоступным архивом: STATE прежний" "$(kv_get "$D/backend/deploy.conf" STATE)" installed

: >"$MOCK_STATE/log"
rc=0
run_deploy reinstall >"$E2E/reinstall0.out" 2>&1 || rc=$?
check "reinstall без слова УДАЛИТЬ отказывает" test "$rc" -ne 0
check "reinstall без подтверждения: тома не удалялись" bash -c '! grep -q " down " "$1"' _ "$MOCK_STATE/log"
SCUD_CONFIRM_WIPE=УДАЛИТЬ run_deploy reinstall >"$E2E/reinstall.out" 2>&1
rc=$?
check_eq "reinstall: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/reinstall.out" >&2
check "reinstall: down -v со всеми профилями" grep -q "compose -p backend --profile backup --profile mqtt-tls --profile monitoring --profile sms down -v --remove-orphans" "$MOCK_STATE/log"
check "reinstall: дамп до удаления" test -n "$(find "$BK" -maxdepth 1 -name '*-pre-reinstall')"
ln_dump=$(grep -n "backup /scripts/backup.sh" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
ln_down=$(grep -n " down -v" "$MOCK_STATE/log" | head -n 1 | cut -d: -f1)
check "reinstall: дамп раньше удаления томов" test "${ln_dump:-0}" -gt 0 -a "${ln_dump:-0}" -lt "${ln_down:-0}"
check_eq "reinstall: секреты сохранены" "$(cat "$D/backend/secrets/jwt_secret")" "$secrets_before"
check_eq "reinstall: STATE" "$(kv_get "$D/backend/deploy.conf" STATE)" installed
check_eq "reinstall: заполненный sms_api_user сохранён" "$(cat "$D/backend/secrets/sms_api_user")" sms-operator

: >"$MOCK_STATE/log"
printf 'wg-psk' >"$D/backend/secrets/wg_preshared_key"
SCUD_CONFIRM_WIPE=УДАЛИТЬ SCUD_KEEP_SECRETS=n run_deploy reinstall >"$E2E/reinstall_nokeep.out" 2>&1
rc=$?
check_eq "reinstall без сохранения секретов: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/reinstall_nokeep.out" >&2
check "reinstall без сохранения секретов: jwt_secret новый" test "$(cat "$D/backend/secrets/jwt_secret")" != "$secrets_before"
check_eq "reinstall без сохранения секретов: sms_api_user перенесён" "$(cat "$D/backend/secrets/sms_api_user")" sms-operator
check_eq "reinstall без сохранения секретов: wg_private_key перенесён" "$(cat "$D/backend/secrets/wg_private_key")" wg-priv
check_eq "reinstall без сохранения секретов: wg_preshared_key перенесён" "$(cat "$D/backend/secrets/wg_preshared_key")" wg-psk
check_eq "reinstall без сохранения секретов: пустой sms_api_password пуст" "$(wc -c <"$D/backend/secrets/sms_api_password" | tr -d ' ')" 0
check_eq "reinstall без сохранения секретов: 26 секретов" "$(find "$D/backend/secrets" -type f | wc -l | tr -d ' ')" 26

D2=$E2E/opt/scud-sms
rm -f "$MOCK_STATE/volumes"
: >"$MOCK_STATE/log"
SCUD_SMS=y SCUD_SMS_API_USER=gw-user SCUD_SMS_API_PASSWORD=gw-pass PATH="$EBIN:$PATH" SCUD_PUBLIC_URL=https://scud.example.com \
    bash "$DEPLOY_SH" install --archive "$E2E/r1/scud-v1.tar.gz" --dir "$D2" --backup-dir "$BK" -y </dev/null >"$E2E/install_sms.out" 2>&1
rc=$?
check_eq "install с SCUD_SMS=y: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/install_sms.out" >&2
check_eq "install с SCUD_SMS=y без WG-значений: STATE" "$(kv_get "$D2/backend/deploy.conf" STATE)" installed
check_eq "install с SCUD_SMS=y без WG-значений: sms нет в PROFILES" "$(kv_get "$D2/backend/deploy.conf" PROFILES)" backup
check_eq "install с SCUD_SMS=y: sms_api_user" "$(cat "$D2/backend/secrets/sms_api_user")" gw-user
check_eq "install с SCUD_SMS=y: sms_api_password" "$(cat "$D2/backend/secrets/sms_api_password")" gw-pass
check "install с SCUD_SMS=y: предупреждение перечисляет недостающее" grep -q "не заполнено secrets/wg_private_key secrets/wg_preshared_key .env:WG_PEER_PUBKEY .env:SMS_GATEWAY_URL" "$E2E/install_sms.out"
check "install с SCUD_SMS=y: подсказка про update --reconfigure" grep -q "update --reconfigure" "$E2E/install_sms.out"
check "install с SCUD_SMS=y: значения секретов не выводятся" bash -c '! grep -q "gw-user\|gw-pass" "$1"' _ "$E2E/install_sms.out"
check "install с SCUD_SMS=y: up без профиля sms" bash -c 'grep -q "compose -p backend --profile backup up -d" "$1" && ! grep -q -- "--profile sms" "$1"' _ "$MOCK_STATE/log"
check "install с SCUD_SMS=y: wg_gateway не запускался" bash -c '! grep -q "wg_gateway" "$1"' _ "$MOCK_STATE/log"

D2_SECRETS=$D2/backend/secrets
printf 'wg-priv' >"$D2_SECRETS/wg_private_key"
printf 'wg-psk' >"$D2_SECRETS/wg_preshared_key"
printf 'WG_PEER_PUBKEY=peerkey\nSMS_GATEWAY_URL=http://wg_gateway:7777\n' >>"$D2/backend/.env"
: >"$MOCK_STATE/log"
SCUD_SMS=y PATH="$EBIN:$PATH" bash "$DEPLOY_SH" update --reconfigure --dir "$D2" --backup-dir "$BK" -y </dev/null >"$E2E/update_sms.out" 2>&1
rc=$?
check_eq "update --reconfigure с SCUD_SMS=y и всеми значениями: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/update_sms.out" >&2
check "update --reconfigure: sms в PROFILES" bash -c '[[ ,$(grep "^PROFILES=" "$1" | cut -d= -f2), == *,sms,* ]]' _ "$D2/backend/deploy.conf"
check "update --reconfigure: без предупреждения о недостающем" bash -c '! grep -q "SMS-шлюз не включён" "$1"' _ "$E2E/update_sms.out"
check "update --reconfigure: up с --profile sms" grep -q "compose -p backend --profile backup --profile sms up -d" "$MOCK_STATE/log"
check "update --reconfigure: ожидание без wg_gateway" bash -c 'l=$(grep -- "--wait --wait-timeout 600 --remove-orphans" "$1") && [ -n "$l" ] && [[ $l != *wg_gateway* ]] && [[ $l == *user_api* ]]' _ "$MOCK_STATE/log"
check "update --reconfigure: wg_gateway запущен отдельно без --wait" grep -q -- "--profile sms up -d --no-build wg_gateway\$" "$MOCK_STATE/log"
check "update --reconfigure: wg_gateway healthy — без предупреждения" bash -c '! grep -q "wg_gateway не стал healthy" "$1"' _ "$E2E/update_sms.out"

touch "$MOCK_STATE/wg_unhealthy"
WG_HEALTH_WAIT_S=0 PATH="$EBIN:$PATH" bash "$DEPLOY_SH" update --dir "$D2" --backup-dir "$BK" -y </dev/null >"$E2E/update_wg_down.out" 2>&1
rc=$?
check_eq "update с неготовым туннелем: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/update_wg_down.out" >&2
check "update с неготовым туннелем: предупреждение" grep -q "wg_gateway не стал healthy" "$E2E/update_wg_down.out"
check_eq "update с неготовым туннелем: STATE" "$(kv_get "$D2/backend/deploy.conf" STATE)" installed
rm -f "$MOCK_STATE/wg_unhealthy"

: >"$D2_SECRETS/wg_private_key"
: >"$MOCK_STATE/log"
PATH="$EBIN:$PATH" bash "$DEPLOY_SH" update --dir "$D2" --backup-dir "$BK" -y </dev/null >"$E2E/update_sms_missing.out" 2>&1
rc=$?
check_eq "update с sms в deploy.conf и пустым ключом: код 0" "$rc" 0
[ "$rc" = 0 ] || tail -n 30 "$E2E/update_sms_missing.out" >&2
check "update с пустым ключом: предупреждение с именем секрета" grep -q "не заполнено secrets/wg_private_key " "$E2E/update_sms_missing.out"
check "update с пустым ключом: up без профиля sms" bash -c 'grep -q "compose -p backend --profile backup up -d --no-build --wait --wait-timeout 600 --remove-orphans" "$1" && ! grep -q -- "--profile sms up -d --no-build --wait --wait-timeout 600 --remove-orphans" "$1"' _ "$MOCK_STATE/log"
check "update с пустым ключом: wg_gateway не запускался" bash -c '! grep -q "up -d --no-build wg_gateway" "$1"' _ "$MOCK_STATE/log"
check "update с пустым ключом: sms остаётся в deploy.conf" bash -c '[[ ,$(grep "^PROFILES=" "$1" | cut -d= -f2), == *,sms,* ]]' _ "$D2/backend/deploy.conf"
printf 'wg-priv' >"$D2_SECRETS/wg_private_key"

if command -v git >/dev/null 2>&1; then
    PR=$TROOT/packimg
    mkdir -p "$PR/backend"
    echo x >"$PR/backend/docker-compose.yaml"
    git init -q "$PR"
    git -C "$PR" add -A
    git -C "$PR" -c user.email=t@example.org -c user.name=t commit -q -m a
    : >"$MOCK_STATE/log"
    (PATH="$EBIN:$PATH" pack_images "$PR" "$(git -C "$PR" rev-parse HEAD)" "$E2E/img.tar") >/dev/null 2>&1
    check "pack_images: сборка с профилем sms" grep -q "compose -p backend --profile backup --profile mqtt-tls --profile monitoring --profile sms build" "$MOCK_STATE/log"
else
    err_t "git не установлен — тест pack_images пропущен"
fi

out=$(PATH="$EBIN:$PATH" bash "$DEPLOY_SH" status --dir "$D" 2>&1)
check_eq "status с моком docker: код 0" "$?" 0
check "status: блок v2 применён" bash -c '[[ $1 == *"scud_notifications: v2-block применён"* ]]' _ "$out"

# ---------------------------------------------------------------------------
echo "==> Тест 10: источник кода для update и перезапуск новой версией"
PT=$TROOT/plan
mkdir -p "$PT/backend/scripts"
ROOT_DIR=$PT
BACKEND_DIR=$PT/backend
CONF_SOURCE=archive
OPT_ARCHIVE=""
CODE_KEEP=""
plan_code_update
check_eq "архив без нового релиза: код не меняется" "$CODE_KEEP" 1
OPT_ARCHIVE=$PT/missing.tar.gz
rc=0
(plan_code_update) >/dev/null 2>&1 || rc=$?
check "несуществующий архив отклоняется до побочных действий" test "$rc" -ne 0
: >"$PT/rel.tar.gz"
OPT_ARCHIVE=$PT/rel.tar.gz
plan_code_update
check_eq "архив указан: код обновляется" "$CODE_KEEP" 0
OPT_ARCHIVE=""
rc=0
(OPT_REF=v9 plan_code_update) >/dev/null 2>&1 || rc=$?
check "архивная установка: --ref без --archive отклоняется" test "$rc" -ne 0

out=$(
    CODE_KEEP=1 NO_BUILD=0 IMAGES_TAR=""
    missing_images() { :; }
    dc() { echo "DC $*"; }
    build_images 2>&1
)
check "код не менялся, образы есть: сборки нет" bash -c '[[ $1 != *"DC build"* ]]' _ "$out"
out=$(
    CODE_KEEP=1 NO_BUILD=0 IMAGES_TAR=""
    missing_images() { echo backend-acm; }
    dc() { echo "DC $*"; }
    build_images 2>&1
)
check "код не менялся, образа нет: сборка" bash -c '[[ $1 == *"DC build"* ]]' _ "$out"

printf '#!/bin/bash\n# --resume-after-fetch\necho "REEXEC $*"\n' >"$BACKEND_DIR/scripts/deploy.sh"
SELF_PATH=$BACKEND_DIR/scripts/deploy.sh
SELF_SUM=$(sha256_stdin <"$DEPLOY_SH")
NO_BUILD=0
out=$(maybe_reexec 2>/dev/null; echo "NO_REEXEC")
check "файл заменён новой версией по тому же пути: перезапуск" bash -c '[[ $1 == *"REEXEC update --resume-after-fetch"* ]]' _ "$out"
SELF_SUM=$(sha256_stdin <"$BACKEND_DIR/scripts/deploy.sh")
out=$(maybe_reexec 2>/dev/null; echo "NO_REEXEC")
check_eq "та же версия: без перезапуска" "$out" NO_REEXEC

# ---------------------------------------------------------------------------
echo ""
echo "Результат: $pass пройдено, $fail провалено."
if [ "$fail" -gt 0 ]; then
    exit 1
fi
