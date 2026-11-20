#!/usr/bin/env bash
# Установка, обновление и переустановка СКУД на сервере. Справка: deploy.sh -h.
# Руководство: backend/DEPLOYMENT.md, раздел «Автоматическая установка и обновление».

DEFAULT_DIR=/opt/scud
DEFAULT_BACKUP_DIR=/var/backups/scud
COMPOSE_PROJECT=backend
MIN_COMPOSE_VERSION=2.20
ALL_PROFILES=backup,mqtt-tls,monitoring,sms
TOOLS_IMAGE=registry.astralinux.ru/library/astra/ubi18-python311:1.8.5
RELEASES_KEEP=3
WG_HEALTH_WAIT_S=${WG_HEALTH_WAIT_S:-90}
SMS_DEFERRED=0

SECRET_NAMES=(
    postgres_user_password postgres_locker_password postgres_history_password
    postgres_auth_password postgres_notification_password postgres_reports_password
    acm_db_password pgb_admin_password redis_password admin_password jwt_secret
    internal_auth_hmac_key internal_otp_hmac_key internal_max_binding_hmac_key
    internal_user_read_hmac_key internal_session_revoke_hmac_key
    internal_step_up_hmac_key internal_history_hmac_key
    mqtt_password max_bot_token postgres_zabbix_password zabbix_db_password
    sms_api_user sms_api_password wg_private_key wg_preshared_key
)
# Выдаются снаружи, не генерируются; пустой файл — функция выключена.
EXTERNAL_SECRETS=(max_bot_token sms_api_user sms_api_password wg_private_key wg_preshared_key)
POSTGRES_SECRETS=(
    postgres_user_password postgres_locker_password postgres_history_password
    postgres_auth_password postgres_notification_password postgres_reports_password
)
# Эти секреты сервисы требуют при APP_ENV=production (read_secret(required=True)).
PROD_REQUIRED_SECRETS=(
    jwt_secret internal_auth_hmac_key internal_otp_hmac_key internal_max_binding_hmac_key
    internal_user_read_hmac_key internal_session_revoke_hmac_key
    internal_step_up_hmac_key internal_history_hmac_key
)
# Совпадает с _KNOWN_PLACEHOLDER_SECRETS (shared/utils) и dev-значениями seed-dev-secrets.sh.
PLACEHOLDER_SECRETS=(
    change-me-in-production-must-be-at-least-32-bytes-long dev-secret-change-in-production
    internal-auth-dev-key-do-not-use-in-prod internal-otp-dev-key-do-not-use-in-prod
    internal-max-binding-dev-key-do-not-use-in-prod internal-user-read-dev-key-do-not-use-in-prod
    internal-session-revoke-dev-key-do-not-use-in-prod internal-step-up-dev-key-do-not-use-in-prod
    internal-history-dev-key-do-not-use-in-prod scud_backend_dev_password
    redis-dev-only-do-not-use-in-prod password changeme acm_ro_dev_password
    pgb_admin_dev_password zabbix_dev_password
)
APP_SERVICES=(user_api locker_api history_api auth_api acm notification_api reports_api)
POSTGRES_SERVICES=(postgres_user postgres_locker postgres_history postgres_auth postgres_notification postgres_reports)
# сервис|база|init-файл относительно backend/|обязаны ли быть блоки
DB_MAP=(
    "postgres_user|scud_users|services/user_api/app/user_data/db/migrations/001_init.sql|0"
    "postgres_locker|scud_lockers|services/locker_api/app/locker_data/db/migrations/001_init.sql|1"
    "postgres_history|scud_history|services/history_api/app/history_data/db/migrations/001_init_history.sql|1"
    "postgres_auth|scud_auth|services/auth_api/app/auth_data/db/migrations/001_init.sql|0"
    "postgres_notification|scud_notifications|services/notification_api/app/notification_data/db/migrations/001_init.sql|1"
    "postgres_reports|scud_reports|services/reports_api/app/reports_data/db/migrations/001_init.sql|0"
)
CONF_KEYS=(SOURCE GIT_URL GIT_REF PROFILES BACKUP_DIR PUBLIC_URL MQTT_TLS_SAN VERSION PREV_VERSION TARGET_VERSION INSTALLED_AT UPDATED_AT STATE)
ENV_KEYS=(
    APP_ENV CORS_ALLOWED_ORIGIN SECURE_COOKIES TRUSTED_PROXY_CIDR ADMIN_PHONE
    MQTT_BIND_ADDR MQTT_TLS_BIND_ADDR MQTT_TLS_CERT_DIR MAX_BOT_USERNAME
    BACKUP_RETENTION_DAYS BACKUP_INTERVAL_SECONDS FRONTEND_PATH
    LOGIN_RATE_LIMIT LOGIN_RATE_WINDOW_SECONDS VERIFY_RATE_LIMIT VERIFY_RATE_WINDOW_SECONDS
)
JOURNAL_DDL='CREATE TABLE IF NOT EXISTS deploy_schema_upgrades (name text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now());'
# база|блок|запрос: блоки с побочными эффектами на данных, которые на установке
# без журнала уже применены вручную; запрос печатает 1, если это так.
LEGACY_PROBES=(
    # intrusion — последнее, что добавлено в блок; to_jsonb не падает, если колонки states ещё нет.
    "scud_lockers|sensor-type-states|SELECT 1 FROM sensor_types st WHERE st.code = 'intrusion' AND to_jsonb(st) ->> 'states' IS NOT NULL;"
)

info() { printf '==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
warn() { printf 'ВНИМАНИЕ: %s\n' "$*" >&2; }
err() { printf 'ОШИБКА: %s\n' "$*" >&2; }
die() {
    err "$*"
    exit 1
}
have() { command -v "$1" >/dev/null 2>&1; }

usage() {
    cat <<'EOF'
Установка и обновление СКУД (backend, фронтенд, БД).

Использование: deploy.sh [команда] [опции]

Команды:
  (без команды)  интерактивное меню; по умолчанию update, если установка найдена, иначе install
  install        чистая установка на новый сервер
  update         обновление с сохранением данных: дамп БД, новые образы, блоки схемы, перезапуск
  reinstall      переустановка с нуля: дамп -> удаление томов -> установка
  rollback       вернуть предыдущую версию кода и образов (БД не трогается)
  backup         разовый дамп всех БД в каталог на хосте
  migrate        только применить блоки схемы БД
  status         версия, состояние сервисов, блоки схемы, последний бэкап
  pack           (на машине разработчика) собрать архив релиза

Опции:
  --dir DIR         каталог установки (по умолчанию /opt/scud; при запуске из
                    существующей установки — она сама)
  --git URL         git-репозиторий (install)
  --ref REF         ветка, тег или коммит (install, update, reinstall; pack: по умолчанию HEAD)
  --archive FILE    архив релиза, собранный командой pack, вместо git
  -y, --yes         без вопросов: значения по умолчанию и сохранённые ответы
  --reconfigure     update: заново задать параметры (текущие значения — по умолчанию)
  --no-build        не собирать образы, использовать уже имеющиеся
  --skip-backup     update, reinstall: не делать дамп БД перед изменениями
  --backup-dir DIR  каталог дампов на хосте (по умолчанию /var/backups/scud)
  --baseline        migrate: только отметить блоки в журнале, не выполняя их
  --with-images     pack: собрать образы и положить images.tar в архив
  -o FILE           pack: путь выходного архива (по умолчанию ./scud-<ref>-<sha>.tar.gz)
  -h, --help        эта справка

Ответы на вопросы можно задать переменными окружения (для автоматизации):
  SCUD_PUBLIC_URL, SCUD_TRUSTED_PROXY_CIDR, SCUD_APP_ENV, SCUD_ADMIN_PHONE,
  SCUD_ADMIN_PASSWORD, SCUD_ADMIN_PASSWORD_GENERATE, SCUD_MQTT_NETWORK, SCUD_MQTT_TLS,
  SCUD_MQTT_TLS_SAN, SCUD_MAX_BOT_TOKEN, SCUD_MAX_BOT_USERNAME, SCUD_BACKUP_DAILY,
  SCUD_BACKUP_RETENTION_DAYS, SCUD_BACKUP_DIR, SCUD_MONITORING, SCUD_SMS,
  SCUD_SMS_API_USER, SCUD_SMS_API_PASSWORD, SCUD_CONFIRM,
  SCUD_KEEP_SECRETS, SCUD_KEEP_CONFIG, SCUD_UPDATE_CODE, SCUD_INSTALL_DOCKER, SCUD_LEGACY_OK,
  SCUD_MENU;
  для reinstall без вопросов: SCUD_CONFIRM_WIPE=УДАЛИТЬ.
  Да/нет — значения y/n. SCUD_DIR, SCUD_GIT_URL, SCUD_REF — умолчания для опций.

Примеры:
  sudo ./deploy.sh install --git https://example.org/scud.git --ref main
  sudo ./deploy.sh install --archive scud-v1.0-1a2b3c4d.tar.gz
  sudo /opt/scud/backend/scripts/deploy.sh update --ref v1.1
  ./backend/scripts/deploy.sh pack --ref v1.1 --with-images
EOF
}

need_value() {
    if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        err "опция $1 требует значение"
        return 1
    fi
}

parse_args() {
    CMD=""
    OPT_DIR=${SCUD_DIR:-}
    OPT_GIT=${SCUD_GIT_URL:-}
    OPT_REF=${SCUD_REF:-}
    OPT_ARCHIVE=""
    OPT_BACKUP_DIR=""
    OPT_OUT=""
    ASSUME_YES=0
    RECONFIGURE=0
    NO_BUILD=0
    SKIP_BACKUP=0
    BASELINE=0
    WITH_IMAGES=0
    RESUME_AFTER_FETCH=0
    SHOW_HELP=0
    while [ "$#" -gt 0 ]; do
        case "$1" in
            install | update | reinstall | rollback | backup | migrate | status | pack)
                if [ -n "$CMD" ]; then
                    err "указано две команды: $CMD и $1"
                    return 2
                fi
                CMD=$1
                ;;
            --dir | --git | --ref | --archive | --backup-dir | -o)
                need_value "$@" || return 2
                case "$1" in
                    --dir) OPT_DIR=$2 ;;
                    --git) OPT_GIT=$2 ;;
                    --ref) OPT_REF=$2 ;;
                    --archive) OPT_ARCHIVE=$2 ;;
                    --backup-dir) OPT_BACKUP_DIR=$2 ;;
                    -o) OPT_OUT=$2 ;;
                esac
                shift
                ;;
            --dir=* | --git=* | --ref=* | --archive=* | --backup-dir=*)
                local opt=${1%%=*} val=${1#*=}
                need_value "$opt" "$val" || return 2
                case "$opt" in
                    --dir) OPT_DIR=$val ;;
                    --git) OPT_GIT=$val ;;
                    --ref) OPT_REF=$val ;;
                    --archive) OPT_ARCHIVE=$val ;;
                    --backup-dir) OPT_BACKUP_DIR=$val ;;
                esac
                ;;
            -y | --yes) ASSUME_YES=1 ;;
            --reconfigure) RECONFIGURE=1 ;;
            --no-build) NO_BUILD=1 ;;
            --skip-backup) SKIP_BACKUP=1 ;;
            --baseline) BASELINE=1 ;;
            --with-images) WITH_IMAGES=1 ;;
            --resume-after-fetch) RESUME_AFTER_FETCH=1 ;;
            -h | --help) SHOW_HELP=1 ;;
            -*)
                err "неизвестная опция: $1 (справка: deploy.sh -h)"
                return 2
                ;;
            *)
                err "неизвестная команда: $1 (справка: deploy.sh -h)"
                return 2
                ;;
        esac
        shift
    done
    if [ "$RECONFIGURE" = 1 ] && [ -n "$CMD" ] && [ "$CMD" != update ]; then
        err "--reconfigure применяется только с update"
        return 2
    fi
    if [ "$BASELINE" = 1 ] && [ "$CMD" != migrate ]; then
        err "--baseline применяется только с migrate"
        return 2
    fi
    if { [ "$WITH_IMAGES" = 1 ] || [ -n "$OPT_OUT" ]; } && [ "$CMD" != pack ]; then
        err "--with-images и -o применяются только с pack"
        return 2
    fi
    if [ -n "$OPT_GIT" ] && [ -n "$OPT_ARCHIVE" ]; then
        err "укажите что-то одно: --git или --archive"
        return 2
    fi
    return 0
}

# --- KEY=VALUE файлы (.env, deploy.conf, VERSION): разбор без source ---

kv_quote() {
    local v=$1
    if [[ $v == *$'\n'* ]]; then
        return 1
    fi
    if [[ $v =~ ^[A-Za-z0-9_./:@+,=*%-]*$ ]]; then
        printf '%s' "$v"
    elif [[ $v == *"'"* ]]; then
        return 1
    else
        printf "'%s'" "$v"
    fi
}

kv_get() {
    local file=$1 key=$2 line val="" found=1
    local re="^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=(.*)$"
    [ -f "$file" ] || return 1
    while IFS= read -r line || [ -n "$line" ]; do
        line=${line%$'\r'}
        if [[ $line =~ $re ]]; then
            val=${BASH_REMATCH[2]}
            found=0
        fi
    done <"$file"
    [ "$found" -eq 0 ] || return 1
    val="${val#"${val%%[![:space:]]*}"}"
    if [[ $val =~ ^\'(.*)\'[[:space:]]*$ ]]; then
        val=${BASH_REMATCH[1]}
    elif [[ $val =~ ^\"(.*)\"[[:space:]]*$ ]]; then
        val=${BASH_REMATCH[1]}
        val=${val//\\\"/\"}
    else
        val=${val%%[[:space:]]#*}
        val="${val%"${val##*[![:space:]]}"}"
    fi
    printf '%s' "$val"
}

kv_has() { kv_get "$1" "$2" >/dev/null; }

kv_set() {
    local file=$1 key=$2 q tmp
    q=$(kv_quote "$3") || {
        err "значение $key содержит недопустимые символы (кавычка или перевод строки)"
        return 1
    }
    if [ -f "$file" ]; then
        tmp=$(umask 077 && mktemp "$file.tmp.XXXXXX") || return 1
        if ! KV_KEY=$key KV_LINE="$key=$q" awk '
            BEGIN { k = ENVIRON["KV_KEY"]; line = ENVIRON["KV_LINE"]; re = "^[[:space:]]*(export[[:space:]]+)?" k "[[:space:]]*=" }
            { s = $0; sub(/\r$/, "", s) }
            s ~ re { if (!done) { print line; done = 1 } next }
            { print }
            END { if (!done) print line }' "$file" >"$tmp" || ! cat "$tmp" >"$file"; then
            rm -f "$tmp"
            return 1
        fi
        rm -f "$tmp"
    else
        (umask 077 && printf '%s=%s\n' "$key" "$q" >"$file")
    fi
}

kv_set_default() {
    if kv_has "$1" "$2"; then
        return 1
    fi
    kv_set "$1" "$2" "$3"
}

# --- Каталоги ---

self_root() {
    local d
    d=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd) || return 1
    if [ -f "$d/backend/docker-compose.yaml" ]; then
        printf '%s' "$d"
    elif [ "$(basename "$d")" = scripts ] && [ -f "$d/../docker-compose.yaml" ]; then
        (cd "$d/../.." && pwd)
    fi
}

abs_path() {
    case "$1" in
        /*) printf '%s' "${1%/}" ;;
        *) printf '%s' "$PWD/${1%/}" ;;
    esac
}

set_root() {
    ROOT_DIR=$1
    BACKEND_DIR=$ROOT_DIR/backend
    CONF_FILE=$BACKEND_DIR/deploy.conf
    ENV_FILE=$BACKEND_DIR/.env
    SECRETS_DIR=$BACKEND_DIR/secrets
    STATE_DIR=$ROOT_DIR/.deploy
}

install_state() {
    if [ -f "$CONF_FILE" ]; then
        echo conf
    elif [ -f "$ENV_FILE" ] || [ -s "$SECRETS_DIR/postgres_user_password" ]; then
        echo legacy
    else
        echo none
    fi
}

resolve_dir() {
    local sr dir
    if [ -n "$OPT_DIR" ]; then
        dir=$(abs_path "$OPT_DIR")
    else
        dir=$DEFAULT_DIR
        sr=$(self_root || true)
        if [ -n "$sr" ] && [ "$CMD" != install ]; then
            set_root "$sr"
            if [ "$(install_state)" != none ]; then
                dir=$sr
            fi
        fi
    fi
    set_root "$dir"
}

# --- Вопросы ---

ask() {
    local var=$1 prompt=$2 def=$3 validator=${4:-valid_any} env="SCUD_$1" val
    if [[ -v $env ]]; then
        val=${!env}
        "$validator" "$val" || die "недопустимое значение $env='$val'"
    elif [ "$ASSUME_YES" = 1 ]; then
        val=$def
        "$validator" "$val" || die "нет допустимого значения по умолчанию для «$prompt»: задайте $env"
    else
        while :; do
            if [ -n "$def" ]; then
                printf '%s [%s]: ' "$prompt" "$def" >&2
            else
                printf '%s: ' "$prompt" >&2
            fi
            if ! IFS= read -r val; then
                printf '\n' >&2
                val=$def
                "$validator" "$val" || die "ввод закончился, а значения по умолчанию для «$prompt» нет: задайте $env"
                break
            fi
            [ -n "$val" ] || val=$def
            if "$validator" "$val"; then
                break
            fi
            warn "недопустимое значение, повторите ввод"
        done
    fi
    printf -v "ANS_$var" '%s' "$val"
}

yn_norm() {
    case "${1,,}" in
        y | yes | д | да) echo y ;;
        n | no | н | нет) echo n ;;
        *) return 1 ;;
    esac
}

valid_yn() { yn_norm "$1" >/dev/null; }

ask_yn() {
    local var=$1 prompt=$2 def=$3 ref
    local hint="y/N"
    [ "$def" = y ] && hint="Y/n"
    ask "$var" "$prompt ($hint)" "$def" valid_yn
    ref="ANS_$var"
    printf -v "ANS_$var" '%s' "$(yn_norm "${!ref}")"
    [ "${!ref}" = y ]
}

confirm() {
    if ! ask_yn CONFIRM "${1:-Продолжить?}" y; then
        die "отменено"
    fi
}

valid_any() { [[ $1 != *$'\n'* ]] && [[ $1 != *"'"* ]]; }
valid_url() { parse_public_url "$1"; }
valid_app_env() { [ "$1" = production ] || [ "$1" = development ]; }
valid_phone() { [[ $1 =~ ^\+[0-9]{10,15}$ ]]; }
valid_days() { [[ $1 =~ ^[0-9]{1,4}$ ]] && [ "$1" -ge 1 ]; }
valid_abs_path() { [[ $1 =~ ^/[A-Za-z0-9._/-]*$ ]]; }
valid_token() { [[ $1 =~ ^[^[:space:]\'\"]*$ ]]; }
valid_cidrs() {
    local t
    local -a list=()
    read -ra list <<<"$1"
    [ "${#list[@]}" -gt 0 ] || return 1
    for t in "${list[@]}"; do
        [[ $t =~ ^[0-9A-Fa-f:.]+(/[0-9]{1,3})?$ ]] || return 1
    done
}
valid_san() {
    local t
    local -a list=()
    read -ra list <<<"$1"
    [ "${#list[@]}" -gt 0 ] || return 1
    for t in "${list[@]}"; do
        [[ $t =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ ]] || return 1
    done
}
valid_password() {
    [ "${#1}" -ge 8 ] || return 1
    [[ $1 =~ ^[[:graph:]]+$ ]] || return 1
    ! is_placeholder "$1"
}

is_ipv4() { [[ $1 =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; }

# PU_SCHEME, PU_HOST, PU_PORT, PU_ORIGIN (для CORS), PU_SECURE (для SECURE_COOKIES).
parse_public_url() {
    local url=${1,,}
    local re='^(https?)://([a-z0-9]([a-z0-9.-]*[a-z0-9])?)(:([0-9]{1,5}))?(/.*)?$'
    [[ $url =~ $re ]] || return 1
    PU_SCHEME=${BASH_REMATCH[1]}
    PU_HOST=${BASH_REMATCH[2]}
    PU_PORT=${BASH_REMATCH[5]}
    if [ -n "$PU_PORT" ]; then
        if [ "$((10#$PU_PORT))" -lt 1 ] || [ "$((10#$PU_PORT))" -gt 65535 ]; then
            return 1
        fi
        PU_PORT=$((10#$PU_PORT))
        if { [ "$PU_SCHEME" = http ] && [ "$PU_PORT" = 80 ]; } || { [ "$PU_SCHEME" = https ] && [ "$PU_PORT" = 443 ]; }; then
            PU_PORT=""
        fi
    fi
    PU_ORIGIN="$PU_SCHEME://$PU_HOST${PU_PORT:+:$PU_PORT}"
    PU_SECURE=false
    if [ "$PU_SCHEME" = https ]; then
        PU_SECURE=true
    fi
}

primary_ipv4() {
    local ip=""
    if have ip; then
        ip=$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')
    fi
    if [ -z "$ip" ] && have hostname; then
        ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    fi
    printf '%s' "${ip:-127.0.0.1}"
}

# --- Значения .env по умолчанию ---

# Новая установка.
install_default() {
    case "$1" in
        APP_ENV) echo production ;;
        CORS_ALLOWED_ORIGIN) echo "" ;;
        SECURE_COOKIES) echo true ;;
        TRUSTED_PROXY_CIDR) echo 127.0.0.1/8 ;;
        ADMIN_PHONE) echo +79000000000 ;;
        MQTT_BIND_ADDR) echo 0.0.0.0 ;;
        MQTT_TLS_BIND_ADDR) echo 127.0.0.1 ;;
        MQTT_TLS_CERT_DIR) echo ./mqtt-tls/certs ;;
        MAX_BOT_USERNAME) echo "" ;;
        BACKUP_RETENTION_DAYS) echo 7 ;;
        BACKUP_INTERVAL_SECONDS) echo 86400 ;;
        FRONTEND_PATH) echo "$ROOT_DIR/frontend" ;;
        LOGIN_RATE_LIMIT | VERIFY_RATE_LIMIT) echo 10 ;;
        LOGIN_RATE_WINDOW_SECONDS | VERIFY_RATE_WINDOW_SECONDS) echo 60 ;;
        *) return 1 ;;
    esac
}

# Отсутствующий в .env ключ уже работает с умолчанием docker-compose.yaml;
# при update дописываем именно его, чтобы поведение не изменилось молча.
compat_default() {
    case "$1" in
        APP_ENV) echo development ;;
        CORS_ALLOWED_ORIGIN) echo "*" ;;
        SECURE_COOKIES) echo true ;;
        TRUSTED_PROXY_CIDR) echo 127.0.0.1/8 ;;
        ADMIN_PHONE) echo +79000000000 ;;
        MQTT_BIND_ADDR | MQTT_TLS_BIND_ADDR) echo 127.0.0.1 ;;
        MQTT_TLS_CERT_DIR) echo ./mqtt-tls/certs ;;
        MAX_BOT_USERNAME) echo "" ;;
        BACKUP_RETENTION_DAYS) echo 7 ;;
        BACKUP_INTERVAL_SECONDS) echo 86400 ;;
        FRONTEND_PATH) echo /root/frontend ;;
        LOGIN_RATE_LIMIT | VERIFY_RATE_LIMIT) echo 100 ;;
        LOGIN_RATE_WINDOW_SECONDS | VERIFY_RATE_WINDOW_SECONDS) echo 60 ;;
        *) return 1 ;;
    esac
}

env_cur() {
    local key=$1 mode=$2 v
    if v=$(kv_get "$ENV_FILE" "$key"); then
        printf '%s' "$v"
    elif [ "$mode" = install ]; then
        install_default "$key"
    else
        compat_default "$key"
    fi
}

# Дописывает отсутствующие ключи; существующие не трогает. ENV_ADDED — что добавлено.
env_merge_defaults() {
    local mode=$1 key val name
    ENV_ADDED=()
    for key in "${ENV_KEYS[@]}"; do
        if [ "$mode" = install ]; then
            val=$(install_default "$key")
        else
            val=$(compat_default "$key")
        fi
        if kv_set_default "$ENV_FILE" "$key" "$val"; then
            ENV_ADDED+=("$key")
        fi
    done
    for name in redis_password mqtt_password acm_db_password; do
        key=${name^^}
        if [ -s "$SECRETS_DIR/$name" ] && kv_set_default "$ENV_FILE" "$key" "$(secret_read "$name")"; then
            ENV_ADDED+=("$key")
        fi
    done
    chmod 0600 "$ENV_FILE"
}

# redis получает пароль только из .env (command в compose), клиенты — из secrets/redis_password.
check_redis_mirror() {
    local envv secret
    secret=$(secret_read redis_password) || return 0
    envv=$(kv_get "$ENV_FILE" REDIS_PASSWORD) || return 0
    if [ "$envv" != "$secret" ]; then
        if ! kv_has "$ENV_FILE" REDIS_URL; then
            warn "REDIS_PASSWORD в .env не совпадает с secrets/redis_password: user_api и acm не смогут подключиться к redis."
            note "Исправление: впишите в $ENV_FILE REDIS_PASSWORD=<содержимое secrets/redis_password> и выполните update."
        fi
    fi
}

warn_insecure_env() {
    local v
    v=$(kv_get "$ENV_FILE" APP_ENV || true)
    if [ "$v" != production ] && [ "$v" != prod ]; then
        if grep -qE 'APP_ENV[=:][[:space:]]*"?prod' "$BACKEND_DIR/docker-compose.override.yaml" 2>/dev/null; then
            note "APP_ENV=production задан в docker-compose.override.yaml"
        else
            warn "APP_ENV=${v:-development}: проверки секретов в режиме production выключены."
        fi
    fi
    v=$(kv_get "$ENV_FILE" CORS_ALLOWED_ORIGIN || true)
    if [ "$v" = "*" ] || [ -z "$v" ]; then
        warn "CORS_ALLOWED_ORIGIN='*' — для боевой эксплуатации задайте адрес сайта (update --reconfigure)."
    fi
}

# --- Секреты ---

is_placeholder() {
    local p
    for p in "${PLACEHOLDER_SECRETS[@]}"; do
        if [ "$1" = "$p" ]; then
            return 0
        fi
    done
    return 1
}

gen_hex() {
    local n=$1 out=""
    if have openssl; then
        out=$(openssl rand -hex "$n" 2>/dev/null | tr -d '\r\n') || out=""
    fi
    if ! [[ $out =~ ^[0-9a-f]+$ ]] || [ "${#out}" -ne $((n * 2)) ]; then
        out=""
        if [ -r /dev/urandom ] && have od; then
            out=$(od -An -tx1 -N"$n" /dev/urandom | tr -d ' \n') || out=""
        fi
    fi
    if ! [[ $out =~ ^[0-9a-f]+$ ]] || [ "${#out}" -ne $((n * 2)) ]; then
        out=""
        if have docker; then
            out=$(docker run --rm "$TOOLS_IMAGE" python3 -c "import secrets; print(secrets.token_hex($n))" 2>/dev/null | tr -d '\r\n') || out=""
        fi
    fi
    if ! [[ $out =~ ^[0-9a-f]+$ ]] || [ "${#out}" -ne $((n * 2)) ]; then
        return 1
    fi
    printf '%s' "$out"
}

secret_bytes() {
    case "$1" in
        jwt_secret | internal_*) echo 32 ;;
        admin_password) echo 12 ;;
        *) echo 16 ;;
    esac
}

secret_read() {
    [ -f "$SECRETS_DIR/$1" ] || return 1
    tr -d '\r\n' <"$SECRETS_DIR/$1"
}

write_secret() {
    local path=$SECRETS_DIR/$1
    if [ -d "$path" ]; then
        rmdir "$path" 2>/dev/null || {
            err "на месте секрета $1 находится непустой каталог: $path"
            return 1
        }
    fi
    (umask 022 && printf '%s' "$2" >"$path.tmp.$$") && chmod 0644 "$path.tmp.$$" && mv -f "$path.tmp.$$" "$path"
}

external_secret_input() {
    case "$1" in
        max_bot_token) printf '%s' "${SECRET_MAX_BOT_TOKEN:-}" ;;
        sms_api_user) printf '%s' "${SECRET_SMS_API_USER:-}" ;;
        sms_api_password) printf '%s' "${SECRET_SMS_API_PASSWORD:-}" ;;
        wg_private_key) printf '%s' "${SECRET_WG_PRIVATE_KEY:-}" ;;
        wg_preshared_key) printf '%s' "${SECRET_WG_PRESHARED_KEY:-}" ;;
    esac
}

# Создаёт только отсутствующие/пустые секреты. SECRET_ADMIN_PASSWORD и
# SECRET_<имя из EXTERNAL_SECRETS> — введённые значения. SECRETS_CREATED — что создано.
ensure_secrets() {
    local name path value
    SECRETS_CREATED=()
    mkdir -p "$SECRETS_DIR" || return 1
    chmod 0700 "$SECRETS_DIR"
    for name in "${SECRET_NAMES[@]}"; do
        path=$SECRETS_DIR/$name
        if [ -f "$path" ] && [ -s "$path" ]; then
            chmod 0644 "$path"
            continue
        fi
        case "$name" in
            admin_password)
                value=${SECRET_ADMIN_PASSWORD:-}
                if [ -z "$value" ]; then
                    value=$(gen_hex "$(secret_bytes "$name")") || {
                        err "не удалось сгенерировать секрет $name (нет openssl, /dev/urandom и docker)"
                        return 1
                    }
                fi
                ;;
            max_bot_token | sms_api_user | sms_api_password | wg_private_key | wg_preshared_key)
                value=$(external_secret_input "$name")
                if [ -z "$value" ] && [ -f "$path" ]; then
                    chmod 0644 "$path"
                    continue
                fi
                ;;
            *)
                value=$(gen_hex "$(secret_bytes "$name")") || {
                    err "не удалось сгенерировать секрет $name (нет openssl, /dev/urandom и docker)"
                    return 1
                }
                ;;
        esac
        if [ -n "$value" ] && is_placeholder "$value"; then
            err "значение секрета $name совпадает с известным dev-значением"
            return 1
        fi
        write_secret "$name" "$value" || return 1
        SECRETS_CREATED+=("$name")
    done
    if [ "${#SECRETS_CREATED[@]}" -gt 0 ]; then
        note "созданы секреты: ${SECRETS_CREATED[*]}"
    fi
}

sms_missing() {
    local name v
    SMS_MISSING=()
    for name in sms_api_user sms_api_password wg_private_key wg_preshared_key; do
        [ -s "$SECRETS_DIR/$name" ] || SMS_MISSING+=("secrets/$name")
    done
    for name in WG_PEER_PUBKEY SMS_GATEWAY_URL; do
        v=$(kv_get "$ENV_FILE" "$name" 2>/dev/null) || v=""
        [ -n "$v" ] || SMS_MISSING+=(".env:$name")
    done
}

# wg_gateway без ключей и WG_PEER_PUBKEY не стартует, поэтому sms попадает в CONF_PROFILES,
# только когда всё заполнено. Режим defer: профиль не берётся на этот запуск, но в deploy.conf остаётся.
gate_sms_profile() {
    has_profile "$CONF_PROFILES" sms || return 0
    sms_missing
    [ "${#SMS_MISSING[@]}" -gt 0 ] || return 0
    warn "SMS-шлюз не включён: не заполнено ${SMS_MISSING[*]} (backend/DEPLOYMENT.md, раздел «SMS-шлюз»). Остальные сервисы запускаются без профиля sms."
    note "Чтобы включить позже: заполните перечисленное в $BACKEND_DIR и выполните deploy.sh update --reconfigure, ответив «да» на вопрос про SMS-шлюз (или SCUD_SMS=y deploy.sh update --reconfigure -y)."
    CONF_PROFILES=$(profiles_without "$CONF_PROFILES" sms)
    if [ "${1:-}" = defer ]; then
        SMS_DEFERRED=1
    fi
}

# Предупреждает о dev-значениях. Возвращает 1, если в production сервис не запустится.
audit_secrets() {
    local app_env=$1 name v bad=0
    for name in "${SECRET_NAMES[@]}"; do
        v=$(secret_read "$name" 2>/dev/null) || continue
        is_placeholder "$v" || continue
        if [[ " ${PROD_REQUIRED_SECRETS[*]} " == *" $name "* ]] && [ "$app_env" = production ]; then
            err "секрет $name содержит dev-значение: при APP_ENV=production сервисы не запустятся."
            bad=1
        else
            warn "секрет $name содержит dev-значение."
        fi
    done
    if [ "$bad" = 1 ]; then
        note "Удалите эти файлы из $SECRETS_DIR, если на них не завязаны данные (HMAC-ключи и jwt_secret безопасно пересоздать), и повторите."
        return 1
    fi
}

# --- deploy.conf ---

reset_conf() {
    local k
    for k in "${CONF_KEYS[@]}"; do
        printf -v "CONF_$k" '%s' ""
    done
}

load_conf() {
    local k v
    reset_conf
    for k in "${CONF_KEYS[@]}"; do
        v=$(kv_get "$CONF_FILE" "$k") || v=""
        printf -v "CONF_$k" '%s' "$v"
    done
}

save_conf() {
    local k ref tmp line
    tmp="$CONF_FILE.tmp.$$"
    (
        umask 077
        : >"$tmp"
        for k in "${CONF_KEYS[@]}"; do
            ref="CONF_$k"
            line=$(kv_quote "${!ref}") || exit 1
            printf '%s=%s\n' "$k" "$line" >>"$tmp"
        done
    ) || {
        rm -f "$tmp"
        err "не удалось записать $CONF_FILE"
        return 1
    }
    mv -f "$tmp" "$CONF_FILE"
    chmod 0600 "$CONF_FILE"
}

has_profile() { [[ ",$1," == *",$2,"* ]]; }

profiles_with() {
    if [ -z "$1" ]; then
        printf '%s' "$2"
    elif has_profile "$1" "$2"; then
        printf '%s' "$1"
    else
        printf '%s,%s' "$1" "$2"
    fi
}

profiles_without() {
    local p out="" list=()
    IFS=, read -ra list <<<"$1"
    for p in ${list[@]+"${list[@]}"}; do
        if [ -n "$p" ] && [ "$p" != "$2" ]; then
            out=$(profiles_with "$out" "$p")
        fi
    done
    printf '%s' "$out"
}

# Имена сервисов compose (stdin) -> список профилей.
profiles_from_services() {
    local svc found=""
    while IFS= read -r svc; do
        case "$svc" in
            backup) found=$(profiles_with "$found" backup) ;;
            mqtt_tls) found=$(profiles_with "$found" mqtt-tls) ;;
            zabbix_server | zabbix_web | zabbix_agent2 | postgres_zabbix | syslog_ng) found=$(profiles_with "$found" monitoring) ;;
            wg_gateway) found=$(profiles_with "$found" sms) ;;
        esac
    done
    printf '%s' "$found"
}

release_info() {
    local file=$1/VERSION
    REL_SHA=$(kv_get "$file" SHA) || return 1
    REL_REF=$(kv_get "$file" REF) || REL_REF=""
    REL_DATE=$(kv_get "$file" DATE) || REL_DATE=""
    [[ $REL_SHA =~ ^[0-9a-f]{40}$ ]] && [ -f "$1/backend/docker-compose.yaml" ]
}

tree_version() {
    if [ "$CONF_SOURCE" = git ]; then
        git -C "$ROOT_DIR" rev-parse HEAD 2>/dev/null
    else
        kv_get "$ROOT_DIR/VERSION" SHA
    fi
}

tree_version_label() {
    if [ "$CONF_SOURCE" = git ]; then
        git -C "$ROOT_DIR" describe --tags --always 2>/dev/null
    elif release_info "$ROOT_DIR"; then
        printf '%s (%s)' "${REL_REF:-?}" "${REL_SHA:0:8}"
    fi
}

infer_legacy_conf() {
    local quiet=${1:-0} cors
    reset_conf
    if [ -d "$ROOT_DIR/.git" ] && have git; then
        CONF_SOURCE=git
        CONF_GIT_URL=$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || true)
        CONF_GIT_REF=$(git -C "$ROOT_DIR" symbolic-ref --short -q HEAD || git -C "$ROOT_DIR" rev-parse HEAD)
        CONF_VERSION=$(git -C "$ROOT_DIR" rev-parse HEAD)
    elif release_info "$ROOT_DIR"; then
        CONF_SOURCE=archive
        CONF_GIT_REF=$REL_REF
        CONF_VERSION=$REL_SHA
    elif [ "$quiet" = 1 ]; then
        CONF_SOURCE="не определён"
    else
        die "не удалось определить источник кода в $ROOT_DIR (нет .git с git или VERSION)"
    fi
    if have docker && docker info >/dev/null 2>&1; then
        CONF_PROFILES=$(docker ps -a --filter "label=com.docker.compose.project=$COMPOSE_PROJECT" \
            --format '{{.Label "com.docker.compose.service"}}' 2>/dev/null | profiles_from_services)
    fi
    CONF_BACKUP_DIR=${OPT_BACKUP_DIR:-$DEFAULT_BACKUP_DIR}
    cors=$(kv_get "$ENV_FILE" CORS_ALLOWED_ORIGIN || true)
    if [ -n "$cors" ] && [ "$cors" != "*" ] && parse_public_url "$cors"; then
        CONF_PUBLIC_URL=$PU_ORIGIN
    fi
    CONF_STATE=installed
    [ "$quiet" = 1 ] && return 0
    warn "в $ROOT_DIR нет backend/deploy.conf: установка сделана вручную или старым скриптом. Параметры определены автоматически:"
    note "источник: $CONF_SOURCE ${CONF_GIT_URL:+$CONF_GIT_URL }${CONF_GIT_REF:+(ref $CONF_GIT_REF)}"
    note "профили compose: ${CONF_PROFILES:-нет}"
    note "каталог дампов: $CONF_BACKUP_DIR"
    if ! ask_yn LEGACY_OK "Параметры верны?" y; then
        die "уточните параметры: deploy.sh update --reconfigure"
    fi
}

load_install_state() {
    case "$(install_state)" in
        conf) load_conf ;;
        legacy) infer_legacy_conf 0 ;;
        *) die "в $ROOT_DIR установка не найдена (нет backend/deploy.conf, .env и секретов). Для новой установки: deploy.sh install" ;;
    esac
    if [ -n "$OPT_BACKUP_DIR" ]; then
        CONF_BACKUP_DIR=$(abs_path "$OPT_BACKUP_DIR")
    fi
    [ -n "$CONF_BACKUP_DIR" ] || CONF_BACKUP_DIR=$DEFAULT_BACKUP_DIR
}

# --- docker compose ---

dc_p() {
    local profs=$1 p
    shift
    local -a args=(compose -p "$COMPOSE_PROJECT") list=()
    IFS=, read -ra list <<<"$profs"
    for p in ${list[@]+"${list[@]}"}; do
        if [ -n "$p" ]; then
            args+=(--profile "$p")
        fi
    done
    (cd "$BACKEND_DIR" && docker "${args[@]}" "$@")
}

dc() { dc_p "$CONF_PROFILES" "$@"; }
dc_all() { dc_p "$ALL_PROFILES" "$@"; }

db_psql() {
    local svc=$1 db=$2
    shift 2
    dc exec -T -e PGOPTIONS='-c client_min_messages=warning' "$svc" psql -U admin -d "$db" -v ON_ERROR_STOP=1 -X -q "$@"
}

docker_ok() { have docker && docker info >/dev/null 2>&1; }

compose_version() {
    local v
    v=$(docker compose version --short 2>/dev/null) || return 1
    v=${v#v}
    v=${v%%[!0-9.]*}
    [ -n "$v" ] || return 1
    printf '%s' "$v"
}

version_ge() {
    local IFS=. i x y
    local -a a b
    read -ra a <<<"$1"
    read -ra b <<<"$2"
    for i in 0 1 2; do
        x=${a[i]:-0}
        y=${b[i]:-0}
        if ((10#$x > 10#$y)); then
            return 0
        fi
        if ((10#$x < 10#$y)); then
            return 1
        fi
    done
    return 0
}

existing_volumes() {
    docker volume ls -q 2>/dev/null | grep -E "^${COMPOSE_PROJECT}_postgres_[a-z]+_data$" || true
}

warn_override() {
    if [ -f "$BACKEND_DIR/docker-compose.override.yaml" ]; then
        warn "найден backend/docker-compose.override.yaml — docker compose применяет его автоматически, его настройки имеют приоритет над .env."
    fi
}

ensure_postgres_up() {
    info "Проверка серверов PostgreSQL"
    dc up -d --no-build --wait --wait-timeout 300 "${POSTGRES_SERVICES[@]}" >/dev/null ||
        die "серверы PostgreSQL не запустились (docker compose -p $COMPOSE_PROJECT ps; logs postgres_*)"
}

# healthcheck wg_gateway ждёт handshake: офлайн-пир не должен валить установку и обновление.
compose_up() {
    local a svc named=0 split_wg=0
    local -a svcs=()
    for a in "$@"; do
        case $a in
            -*) ;;
            *) named=1 ;;
        esac
    done
    if [ "$named" = 0 ] && has_profile "$CONF_PROFILES" sms; then
        split_wg=1
        while IFS= read -r svc; do
            if [ -n "$svc" ] && [ "$svc" != wg_gateway ]; then
                svcs+=("$svc")
            fi
        done < <(dc config --services 2>/dev/null)
        if [ "${#svcs[@]}" -eq 0 ]; then
            err "docker compose config --services не вернул список сервисов"
            return 1
        fi
    fi
    if ! dc up -d --no-build --wait --wait-timeout 600 "$@" ${svcs[@]+"${svcs[@]}"}; then
        err "сервисы не перешли в состояние healthy"
        show_unhealthy
        return 1
    fi
    if [ "$split_wg" = 1 ]; then
        start_wg_gateway
    fi
}

start_wg_gateway() {
    local waited=0 health
    # rollback на релиз без SMS: сервиса нет в compose-файле
    dc config --services 2>/dev/null | grep -qx wg_gateway || return 0
    if ! dc up -d --no-build wg_gateway; then
        warn "не удалось запустить wg_gateway: SMS недоступны, остальные сервисы работают"
        return 0
    fi
    while :; do
        health=$(dc ps --format '{{.Health}}' wg_gateway 2>/dev/null || true)
        if [ "$health" = healthy ]; then
            return 0
        fi
        if [ "$waited" -ge "$WG_HEALTH_WAIT_S" ]; then
            break
        fi
        sleep 5
        waited=$((waited + 5))
    done
    warn "wg_gateway не стал healthy (туннель без handshake: пир офлайн или неверные WG_PEER_PUBKEY/ключи): SMS недоступны, остальные сервисы работают."
    note "Проверьте: docker compose -p $COMPOSE_PROJECT --profile sms logs wg_gateway; docker compose -p $COMPOSE_PROJECT --profile sms exec wg_gateway wg show wg0"
}

# Compose не считает сервисы выключенных профилей сиротами: --remove-orphans их не трогает.
stop_disabled_profile_services() {
    local all cur existing svc
    local -a drop=()
    all=$(dc_all config --services 2>/dev/null) || return 0
    cur=$(dc config --services 2>/dev/null) || return 0
    existing=$(dc_all ps -a --services 2>/dev/null) || existing=""
    while IFS= read -r svc; do
        [ -n "$svc" ] || continue
        if ! grep -qxF -- "$svc" <<<"$cur" && grep -qxF -- "$svc" <<<"$existing"; then
            drop+=("$svc")
        fi
    done <<<"$all"
    [ "${#drop[@]}" -gt 0 ] || return 0
    info "Остановка сервисов выключенных профилей: ${drop[*]}"
    if ! dc_all stop "${drop[@]}" || ! dc_all rm -f "${drop[@]}"; then
        warn "не удалось остановить ${drop[*]} — остановите вручную: docker compose -p $COMPOSE_PROJECT --profile <профиль> rm -sf <сервис>"
    fi
}

show_unhealthy() {
    local svc state health
    dc ps -a || true
    while read -r svc state health; do
        if [ "$state" = running ] && { [ -z "$health" ] || [ "$health" = healthy ]; }; then
            continue
        fi
        printf '\n--- %s (%s %s), последние 50 строк журнала ---\n' "$svc" "$state" "$health" >&2
        dc logs --no-color --tail 50 "$svc" >&2 || true
    done < <(dc ps -a --format '{{.Service}} {{.State}} {{.Health}}' 2>/dev/null || true)
}

norm_image() {
    case "$1" in
        *:*) printf '%s' "$1" ;;
        *) printf '%s:latest' "$1" ;;
    esac
}

rollback_tag() {
    local n
    n=$(norm_image "$1")
    printf '%s:rollback' "${n%:*}"
}

# Собираемые проектом образы; внешние (registry/...) от версии кода не зависят.
local_built_images() {
    dc_all config --images 2>/dev/null | grep -v / | LC_ALL=C sort -u
}

tag_rollback_images() {
    local img n imgs listfile=$STATE_DIR/rollback-images
    imgs=$(local_built_images) || imgs=""
    if [ -z "$imgs" ]; then
        err "docker compose config --images не вернул список собираемых образов"
        return 1
    fi
    mkdir -p "$STATE_DIR" || return 1
    : >"$listfile.tmp" || return 1
    while IFS= read -r img; do
        [ -n "$img" ] || continue
        n=$(norm_image "$img")
        if docker image inspect "$n" >/dev/null 2>&1; then
            docker tag "$n" "$(rollback_tag "$n")" || return 1
            printf '%s\n' "$n" >>"$listfile.tmp"
        fi
    done <<<"$imgs"
    if [ ! -s "$listfile.tmp" ]; then
        rm -f "$listfile.tmp"
        err "ни одного образа проекта нет локально — пометить для отката нечего"
        return 1
    fi
    mv -f "$listfile.tmp" "$listfile"
    note "образы помечены тегом :rollback ($(wc -l <"$listfile"))"
}

restore_rollback_images() {
    local n
    while IFS= read -r n; do
        [ -n "$n" ] || continue
        docker tag "$(rollback_tag "$n")" "$n" || return 1
    done <"$STATE_DIR/rollback-images"
}

missing_images() {
    local img
    while IFS= read -r img; do
        [ -n "$img" ] || continue
        docker image inspect "$(norm_image "$img")" >/dev/null 2>&1 || printf '%s\n' "$img"
    done < <(dc config --images 2>/dev/null | LC_ALL=C sort -u)
}

build_images() {
    local missing
    if [ -n "${IMAGES_TAR:-}" ] && [ -f "$IMAGES_TAR" ]; then
        info "Загрузка образов из архива релиза"
        docker load -i "$IMAGES_TAR" || die "docker load не удался"
        return 0
    fi
    if [ "${CODE_KEEP:-0}" = 1 ] && [ "$NO_BUILD" != 1 ] && [ -z "$(missing_images)" ]; then
        note "код не менялся — сборка не нужна, используются имеющиеся образы"
        return 0
    fi
    if [ "$NO_BUILD" = 1 ]; then
        missing=$(missing_images)
        if [ -n "$missing" ]; then
            die "--no-build: нет образов: $(printf '%s' "$missing" | tr '\n' ' ')"
        fi
        note "сборка пропущена (--no-build)"
        return 0
    fi
    info "Сборка образов (первая сборка занимает 5–30 минут)"
    if dc build; then
        return 0
    fi
    warn "сборка не удалась, повторная попытка"
    if dc build; then
        return 0
    fi
    err "сборка образов не удалась."
    note "Сборке нужен доступ к registry.astralinux.ru и к apt-зеркалу Astra."
    note "Без доступа: соберите на другой машине архив с образами (deploy.sh pack --with-images)"
    note "и запустите с --archive, либо используйте --no-build, если образы уже есть."
    exit 1
}

# --- Схема БД: блоки «-- имя:begin» … «-- имя:end» в 001_init*.sql ---

# Режимы: all | names | one. Код 1 — блоков (или нужного блока) нет, 2 — нарушена разметка.
_schema_awk() {
    awk -v mode="$1" -v want="${3:-}" '
        { sub(/\r$/, "") }
        /^-- [a-z0-9-]+:begin$/ {
            if (open != "") { bad = 1; exit }
            open = substr($0, 4, length($0) - 9)
            if (open in seen) { bad = 1; exit }
            seen[open] = 1
            found++
            if (mode == "names") print open
        }
        open != "" && (mode == "all" || (mode == "one" && open == want)) { print; hit = 1 }
        /^-- [a-z0-9-]+:end$/ {
            name = substr($0, 4, length($0) - 7)
            if (name != open) { bad = 1; exit }
            open = ""
        }
        END {
            if (bad || open != "") exit 2
            if ((mode == "one" && !hit) || (mode != "one" && !found)) exit 1
        }' "$2"
}

_schema_call() {
    local rc=0
    if [ ! -f "$2" ]; then
        err "нет файла $2"
        return 1
    fi
    _schema_awk "$@" || rc=$?
    if [ "$rc" -eq 2 ]; then
        err "нарушена разметка блоков схемы в $2 (вложенный, незакрытый или повторяющийся блок)"
    fi
    return "$rc"
}

schema_blocks() { _schema_call all "$1"; }
schema_block_names() { _schema_call names "$1"; }
schema_block() { _schema_call one "$2" "$1"; }

sha256_stdin() {
    if have sha256sum; then
        sha256sum | awk '{print $1}'
    elif have shasum; then
        shasum -a 256 | awk '{print $1}'
    else
        openssl dgst -sha256 | awk '{print $NF}'
    fi
}

block_checksum() { schema_block "$1" "$2" | sha256_stdin; }

# Строки «имя состояние» по файлу и журналу («имя checksum» на строку).
schema_status() {
    local file=$1 journal=$2 name sum old
    while IFS= read -r name; do
        [ -n "$name" ] || continue
        sum=$(block_checksum "$name" "$file")
        old=$(printf '%s\n' "$journal" | awk -v n="$name" '$1 == n { print $2 }')
        if [ -z "$old" ]; then
            printf '%s ожидает\n' "$name"
        elif [ "$old" = "$sum" ]; then
            printf '%s применён\n' "$name"
        else
            printf '%s изменён\n' "$name"
        fi
    done < <(schema_block_names "$file" 2>/dev/null || true)
}

apply_schema_upgrades() {
    local baseline=0 entry svc db rel req file names rc journal name sum old probe
    if [ "${1:-}" = --baseline ]; then
        baseline=1
    fi
    for entry in "${DB_MAP[@]}"; do
        IFS='|' read -r svc db rel req <<<"$entry"
        file=$BACKEND_DIR/$rel
        info "Схема $db"
        if [ ! -f "$file" ]; then
            err "нет файла $rel"
            return 1
        fi
        rc=0
        names=$(schema_block_names "$file") || rc=$?
        if [ "$rc" -eq 2 ]; then
            return 1
        fi
        if [ "$rc" -eq 1 ] && [ "$req" = 1 ]; then
            err "в $rel не найдено ни одного блока схемы — файл повреждён или не той версии"
            return 1
        fi
        printf '%s\n' "$JOURNAL_DDL" | db_psql "$svc" "$db" || {
            err "не удалось создать журнал deploy_schema_upgrades в $db"
            return 1
        }
        if [ -z "$names" ]; then
            note "блоков нет"
            continue
        fi
        journal=$(printf '%s\n' "SELECT name || ' ' || checksum FROM deploy_schema_upgrades;" | db_psql "$svc" "$db" -tA) || {
            err "не удалось прочитать журнал deploy_schema_upgrades в $db"
            return 1
        }
        while IFS= read -r name; do
            sum=$(block_checksum "$name" "$file") || return 1
            old=$(printf '%s\n' "$journal" | awk -v n="$name" '$1 == n { print $2 }')
            if [ "$old" = "$sum" ]; then
                note "= $name: уже применён"
                continue
            fi
            probe=""
            if [ "$baseline" = 0 ] && [ -z "$old" ]; then
                probe=$(legacy_probe "$db" "$name")
            fi
            if [ "$baseline" = 1 ]; then
                note "+ $name: отмечен как применённый (без выполнения)"
            elif [ -n "$probe" ] && [ "$(q_scalar "$svc" "$db" "$probe")" = 1 ]; then
                note "+ $name: отмечен как применённый без выполнения — изменения уже есть в БД (установка до журнала)"
            else
                if [ -n "$old" ]; then
                    note "~ $name: блок изменился с прошлого применения — выполняется повторно"
                fi
                if ! schema_block "$name" "$file" | db_psql "$svc" "$db"; then
                    err "блок $name в $db завершился ошибкой. Блоки идемпотентны: после исправления причины повторите запуск."
                    return 1
                fi
                note "+ $name: применён"
            fi
            printf '%s\n' "INSERT INTO deploy_schema_upgrades (name, checksum) VALUES (:'n', :'c') ON CONFLICT (name) DO UPDATE SET checksum = EXCLUDED.checksum, applied_at = now();" |
                db_psql "$svc" "$db" -v "n=$name" -v "c=$sum" || {
                err "не удалось записать $name в журнал $db"
                return 1
            }
        done <<<"$names"
    done
}

legacy_probe() {
    local entry pdb pname sql
    for entry in "${LEGACY_PROBES[@]}"; do
        IFS='|' read -r pdb pname sql <<<"$entry"
        if [ "$pdb" = "$1" ] && [ "$pname" = "$2" ]; then
            printf '%s' "$sql"
            return 0
        fi
    done
}

# Однофайловый bind-mount (init-user-db.sh, секреты) после замены файла на хосте
# показывает контейнеру прежнее содержимое, пока контейнер не перезапущен.
restart_postgres_user() {
    info "Перезапуск postgres_user (актуальные init-user-db.sh и секреты)"
    if ! dc restart postgres_user >/dev/null || ! dc up -d --no-build --wait --wait-timeout 300 postgres_user >/dev/null; then
        err "postgres_user не перешёл в состояние healthy после перезапуска"
        return 1
    fi
}

sync_acm_role() {
    local script=$BACKEND_DIR/init-scripts/init-user-db.sh
    if ! grep -q SKIP_MIGRATIONS "$script" 2>/dev/null; then
        warn "init-user-db.sh этой версии не поддерживает SKIP_MIGRATIONS — синхронизация роли acm_ro пропущена"
        return 0
    fi
    info "Синхронизация роли acm_ro"
    dc exec -T -e SKIP_MIGRATIONS=1 postgres_user bash -s <"$script" >/dev/null ||
        {
            err "не удалось обновить роль acm_ro в scud_users"
            return 1
        }
}

q_scalar() {
    printf '%s\n' "$3" | db_psql "$1" "$2" -tA | tr -d '[:space:]'
}

# Старше cc41adc (2026-06-17) блоки схемы не догоняют.
floor_guard() {
    local -a missing=()
    local name out
    for name in "${POSTGRES_SECRETS[@]}"; do
        [ -s "$SECRETS_DIR/$name" ] || missing+=("секрет secrets/$name")
    done
    out=$(q_scalar postgres_user scud_users "SELECT 1 FROM pg_roles WHERE rolname = 'acm_ro';") || die "не удалось выполнить запрос к scud_users"
    [ "$out" = 1 ] || missing+=("роль acm_ro в scud_users")
    out=$(q_scalar postgres_history scud_history "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'security_events' AND column_name = 'event_uid';") ||
        die "не удалось выполнить запрос к scud_history"
    [ "$out" = 1 ] || missing+=("колонка security_events.event_uid в scud_history")
    out=$(q_scalar postgres_locker scud_lockers "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' AND table_name = 'lockers' AND column_name = 'relay_id';") ||
        die "не удалось выполнить запрос к scud_lockers"
    [ "$out" = 1 ] || missing+=("колонка lockers.relay_id в scud_lockers")
    if [ "${#missing[@]}" -gt 0 ]; then
        err "база данных создана версией старше поддерживаемой (до 2026-06-17). Не найдено:"
        for name in "${missing[@]}"; do
            note "- $name"
        done
        note "Автоматическое обновление такой установки невозможно: перенесите данные вручную"
        note "(backend/DEPLOYMENT.md, «Обновление существующего развёртывания») или выполните reinstall."
        return 1
    fi
}

pending_commands_count() {
    q_scalar postgres_locker scud_lockers "SELECT count(*) FROM locker_commands WHERE status IN ('pending', 'sent', 'delivered') AND created_at < now() - interval '2 minutes';" 2>/dev/null || true
}

# --- Резервные копии ---

verify_dumps() {
    local dir=$1 entry svc db rel req ok=0
    local -a files
    for entry in "${DB_MAP[@]}"; do
        IFS='|' read -r svc db rel req <<<"$entry"
        files=("$dir/$db"-*.dump)
        if [ -s "${files[0]}" ]; then
            ok=$((ok + 1))
        else
            err "нет дампа $db в $dir"
        fi
    done
    [ "$ok" -eq "${#DB_MAP[@]}" ]
}

save_config_copy() {
    local dir=$1 p
    local -a items=()
    for p in .env deploy.conf secrets docker-compose.override.yaml mqtt-tls/ca mqtt-tls/server mqtt-tls/certs; do
        if [ -e "$BACKEND_DIR/$p" ]; then
            items+=("$p")
        fi
    done
    [ "${#items[@]}" -gt 0 ] || return 0
    (umask 077 && tar -C "$BACKEND_DIR" -czf "$dir/config.tar.gz" "${items[@]}") || return 1
    chmod 0600 "$dir/config.tar.gz"
}

# DUMP_DIR — каталог с дампами 6 БД и config.tar.gz (.env, секреты, сертификаты).
dump_databases() {
    local label=$1 profs uid base n=1
    profs=$(profiles_with "$CONF_PROFILES" backup)
    (umask 077 && mkdir -p "$CONF_BACKUP_DIR") || return 1
    chmod 0700 "$CONF_BACKUP_DIR" 2>/dev/null || true
    base=$CONF_BACKUP_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$label
    DUMP_DIR=$base
    while ! mkdir "$DUMP_DIR" 2>/dev/null; do
        n=$((n + 1))
        [ "$n" -le 20 ] || return 1
        DUMP_DIR=$base-$n
    done
    info "Дамп всех БД в $DUMP_DIR"
    uid=$(dc_p "$profs" run --rm --no-deps -T --entrypoint id backup -u 2>/dev/null | tr -d '[:space:]') || uid=""
    if [[ $uid =~ ^[0-9]+$ ]] && [ "$(id -u)" = 0 ]; then
        chown "$uid" "$DUMP_DIR" && chmod 0700 "$DUMP_DIR"
    else
        chmod 0777 "$DUMP_DIR"
    fi
    if ! dc_p "$profs" run --rm --no-deps -T -v "$DUMP_DIR:/backups" backup /scripts/backup.sh; then
        err "backup.sh завершился с ошибкой"
        return 1
    fi
    verify_dumps "$DUMP_DIR" || return 1
    chmod 0600 "$DUMP_DIR"/*.dump 2>/dev/null || true
    chmod 0700 "$DUMP_DIR" 2>/dev/null || true
    save_config_copy "$DUMP_DIR" || {
        err "не удалось сохранить копию .env и секретов"
        return 1
    }
    note "дампы и копия конфигурации: $DUMP_DIR"
}

print_restore_hint() {
    local dir=${1:-<каталог дампа>}
    note "Восстановление одной БД из дампа (пример для scud_lockers; остальные — по аналогии,"
    note "пароль — secrets/postgres_<сервис>_password; см. DEPLOYMENT.md, «Disaster recovery»):"
    note "  cd $BACKEND_DIR && docker compose -p $COMPOSE_PROJECT stop locker_api"
    note "  docker compose -p $COMPOSE_PROJECT --profile backup run --rm --no-deps -v $dir:/restore:ro backup \\"
    note "    sh -c 'PGPASSWORD=\$(cat /run/secrets/postgres_locker_password) pg_restore -h postgres_locker \\"
    note "    -U admin -d scud_lockers --clean --if-exists /restore/scud_lockers-<время>.dump'"
}

# --- Код: архив релиза ---

is_protected_path() {
    case "$1" in
        backend/.env | backend/.env.* | backend/deploy.conf | backend/docker-compose.override.yaml) return 0 ;;
        backend/secrets | backend/secrets/* | backend/mqtt-tls/ca/* | backend/mqtt-tls/server/* | backend/mqtt-tls/certs/*) return 0 ;;
        .deploy | .deploy/* | .git | .git/*) return 0 ;;
    esac
    return 1
}

release_manifest() {
    local p
    [ -d "$1" ] || return 1
    (cd "$1" && find . \( -type f -o -type l \) | sed 's|^\./||') | while IFS= read -r p; do
        if [ "$p" = images.tar ] || is_protected_path "$p"; then
            continue
        fi
        printf '%s\n' "$p"
    done | LC_ALL=C sort
}

# Удаляет из DEST файлы прошлого релиза, которых нет в новом; локальные файлы не трогает.
apply_manifest_removals() {
    local dest=$1 old=$2 new=$3 rel d
    if [ ! -s "$new" ]; then
        err "список файлов нового релиза пуст — удаление файлов прежней версии отменено"
        return 1
    fi
    awk 'FILENAME == ARGV[1] { keep[$0] = 1; next } !($0 in keep)' "$new" "$old" | while IFS= read -r rel; do
        [ -n "$rel" ] || continue
        case "$rel" in
            /* | .. | ../* | */../* | */..) continue ;;
        esac
        if is_protected_path "$rel"; then
            continue
        fi
        if [ -f "$dest/$rel" ] || [ -L "$dest/$rel" ]; then
            rm -f -- "$dest/$rel"
            note "удалён файл прежней версии: $rel"
            d=$(dirname "$rel")
            while [ "$d" != . ] && rmdir "$dest/$d" 2>/dev/null; do
                d=$(dirname "$d")
            done
        fi
    done
}

prune_releases() {
    local keep=$1 f n=0
    while IFS= read -r f; do
        n=$((n + 1))
        if [ "$n" -gt "$RELEASES_KEEP" ] && [ "$(basename "$f" .tar.gz)" != "$keep" ]; then
            rm -f -- "$f"
        fi
    done < <(ls -1t "$STATE_DIR"/releases/*.tar.gz 2>/dev/null || true)
}

# Ставит дерево релиза SRC в ROOT_DIR (SRC может совпадать с ROOT_DIR).
install_tree() {
    local src=$1 sha=$2 new
    mkdir -p "$ROOT_DIR" "$STATE_DIR/releases" || return 1
    chmod 0700 "$STATE_DIR"
    new=$STATE_DIR/MANIFEST.new
    if ! release_manifest "$src" >"$new" || [ ! -s "$new" ]; then
        rm -f "$new"
        err "не удалось составить список файлов релиза $src (или он пуст)"
        return 1
    fi
    if [ "$src" != "$ROOT_DIR" ]; then
        if [ -f "$STATE_DIR/MANIFEST" ]; then
            apply_manifest_removals "$ROOT_DIR" "$STATE_DIR/MANIFEST" "$new" || return 1
        fi
        (cd "$src" && tar -cf - -T "$new") | (cd "$ROOT_DIR" && tar -xpf -) || {
            err "не удалось скопировать файлы релиза в $ROOT_DIR"
            return 1
        }
    fi
    mv -f "$new" "$STATE_DIR/MANIFEST"
    tar -C "$ROOT_DIR" -czf "$STATE_DIR/releases/$sha.tar.gz" -T "$STATE_DIR/MANIFEST" || return 1
    prune_releases "${CONF_PREV_VERSION:-}"
}

# RELEASE_DIR — корень распакованного релиза, IMAGES_TAR — образы (если есть).
extract_archive() {
    local file=$1 stage d
    [ -f "$file" ] || die "нет файла архива: $file"
    mkdir -p "$STAGE_BASE" || die "не удалось создать $STAGE_BASE"
    stage=$(mktemp -d "$STAGE_BASE/release.XXXXXX") || die "не удалось создать временный каталог"
    CLEANUP_DIRS+=("$stage")
    info "Распаковка $file"
    tar -xzf "$file" -C "$stage" || die "архив повреждён: $file"
    RELEASE_DIR=""
    if [ -f "$stage/VERSION" ]; then
        RELEASE_DIR=$stage
    else
        for d in "$stage"/*/; do
            if [ -f "$d/VERSION" ]; then
                RELEASE_DIR=${d%/}
                break
            fi
        done
    fi
    if [ -z "$RELEASE_DIR" ] || ! release_info "$RELEASE_DIR"; then
        die "$file — не архив релиза СКУД (нет корректного VERSION или backend/docker-compose.yaml)"
    fi
    IMAGES_TAR=""
    if [ -f "$RELEASE_DIR/images.tar" ]; then
        IMAGES_TAR=$RELEASE_DIR/images.tar
    fi
    note "релиз ${REL_REF:-?} (${REL_SHA:0:8}, ${REL_DATE:-?})"
}

# --- Код: git ---

git_dirty() { [ -n "$(git -C "$1" status --porcelain --untracked-files=no 2>/dev/null)" ]; }

git_ref_target() {
    if git -C "$ROOT_DIR" rev-parse -q --verify "refs/remotes/origin/$1^{commit}" >/dev/null; then
        printf 'origin/%s' "$1"
    elif git -C "$ROOT_DIR" rev-parse -q --verify "$1^{commit}" >/dev/null; then
        printf '%s' "$1"
    else
        return 1
    fi
}

git_checkout_ref() {
    local target
    target=$(git_ref_target "$1") || die "ревизия '$1' не найдена в репозитории"
    git -C "$ROOT_DIR" checkout -q --detach "$target" || die "git checkout $target не удался"
}

git_update_code() {
    local ref=${1:-$CONF_GIT_REF}
    have git || die "нужен git"
    [ -n "$ref" ] || die "не задана ревизия: укажите --ref"
    if git_dirty "$ROOT_DIR"; then
        err "в $ROOT_DIR изменены отслеживаемые git файлы:"
        git -C "$ROOT_DIR" status --short --untracked-files=no >&2
        die "закоммитьте или отмените изменения (локальные настройки — в .env или docker-compose.override.yaml)"
    fi
    if [ -n "$OPT_GIT" ]; then
        git -C "$ROOT_DIR" remote set-url origin "$OPT_GIT"
        CONF_GIT_URL=$OPT_GIT
    fi
    info "Получение кода: git fetch"
    if ! git -C "$ROOT_DIR" fetch --prune --tags origin; then
        if git_ref_target "$ref" >/dev/null; then
            warn "git fetch не удался — используется ревизия '$ref', которая уже есть в локальном репозитории"
        else
            die "git fetch не удался, а ревизии '$ref' локально нет. Альтернатива: соберите архив (deploy.sh pack) и обновите с --archive"
        fi
    fi
    git_checkout_ref "$ref"
    CONF_GIT_REF=$ref
    note "код: $(tree_version_label)"
}

# --- Код для update/reinstall ---

# Проверяет источник нового кода до дампа и тегов отката. Установка из архива без
# нового релиза обновляется на месте: это перенастройка/переразвёртывание текущей версии.
plan_code_update() {
    local sr
    CODE_KEEP=0
    if [ -n "$OPT_ARCHIVE" ]; then
        [ -f "$(abs_path "$OPT_ARCHIVE")" ] || die "нет файла архива: $OPT_ARCHIVE"
    elif [ "$CONF_SOURCE" = archive ]; then
        if [ -n "$OPT_REF" ] || [ -n "$OPT_GIT" ]; then
            die "установка из архива: --ref/--git здесь не работают — соберите архив нужной версии (deploy.sh pack --ref ...) и обновите с --archive"
        fi
        sr=$(self_root || true)
        if [ -z "$sr" ] || [ "$sr" = "$ROOT_DIR" ] || ! release_info "$sr"; then
            CODE_KEEP=1
        fi
    elif git_dirty "$ROOT_DIR"; then
        err "в $ROOT_DIR изменены отслеживаемые git файлы:"
        git -C "$ROOT_DIR" status --short --untracked-files=no >&2
        die "закоммитьте или отмените изменения (локальные настройки — в .env или docker-compose.override.yaml)"
    fi
}

fetch_code_update() {
    local sr
    if [ "${CODE_KEEP:-0}" = 1 ]; then
        note "код не меняется: $(tree_version_label)"
        return 0
    fi
    if [ -n "$OPT_ARCHIVE" ]; then
        extract_archive "$(abs_path "$OPT_ARCHIVE")"
    elif [ "$CONF_SOURCE" = archive ]; then
        sr=$(self_root || true)
        if [ -n "$sr" ] && [ "$sr" != "$ROOT_DIR" ] && release_info "$sr"; then
            RELEASE_DIR=$sr
            IMAGES_TAR=""
            if [ -f "$sr/images.tar" ]; then
                IMAGES_TAR=$sr/images.tar
            fi
            note "релиз из $sr: ${REL_REF:-?} (${REL_SHA:0:8})"
        else
            die "установка из архива: укажите новый релиз --archive FILE (или запустите deploy.sh из распакованного нового релиза)"
        fi
    else
        git_update_code "${OPT_REF:-$CONF_GIT_REF}"
        return 0
    fi
    if [ "$CONF_SOURCE" = git ]; then
        warn "установка переводится из режима git в режим архива"
    fi
    install_tree "$RELEASE_DIR" "$REL_SHA" || die "не удалось обновить файлы в $ROOT_DIR"
    CONF_SOURCE=archive
    CONF_GIT_REF=$REL_REF
}

# --- Параметры установки ---

ask_config() {
    local mode=$1 d_url cors d_net d_tls d_daily d_mon d_sms
    info "Параметры установки"
    d_url=$CONF_PUBLIC_URL
    if [ -z "$d_url" ]; then
        cors=$(kv_get "$ENV_FILE" CORS_ALLOWED_ORIGIN || true)
        if [ -n "$cors" ] && [ "$cors" != "*" ] && parse_public_url "$cors"; then
            d_url=$PU_ORIGIN
        else
            d_url="http://$(primary_ipv4)"
        fi
    fi
    ask PUBLIC_URL "Адрес сайта, как его открывают пользователи (http(s)://хост[:порт])" "$d_url" valid_url
    parse_public_url "$ANS_PUBLIC_URL"
    if [ "$PU_SECURE" = false ]; then
        warn "адрес без https: cookie будут без флага Secure. Для боевой эксплуатации поставьте TLS-терминатор (DEPLOYMENT.md, раздел TLS) и укажите https://."
    fi
    ask TRUSTED_PROXY_CIDR "IP/подсеть TLS-терминатора перед сервером (через пробел)" "$(env_cur TRUSTED_PROXY_CIDR "$mode")" valid_cidrs
    ask APP_ENV "Режим: production (боевой) или development (стенд)" "$(env_cur APP_ENV "$mode")" valid_app_env
    ask ADMIN_PHONE "Телефон администратора (+7XXXXXXXXXX)" "$(env_cur ADMIN_PHONE "$mode")" valid_phone
    if [ "$mode" != install ] && kv_has "$ENV_FILE" ADMIN_PHONE && [ "$ANS_ADMIN_PHONE" != "$(kv_get "$ENV_FILE" ADMIN_PHONE)" ]; then
        warn "смена телефона создаст ВТОРОГО администратора с паролем из secrets/admin_password; прежний останется."
    fi
    SECRET_ADMIN_PASSWORD=""
    if [ ! -s "$SECRETS_DIR/admin_password" ] || [ "${FORCE_ADMIN_QUESTION:-0}" = 1 ]; then
        ask_admin_password
    fi
    d_net=n
    if [ "$(env_cur MQTT_BIND_ADDR "$mode")" = 0.0.0.0 ]; then
        d_net=y
    fi
    ask_yn MQTT_NETWORK "Платы подключаются к серверу по сети (MQTT 1883 на всех интерфейсах)?" "$d_net" || true
    d_tls=n
    if has_profile "$CONF_PROFILES" mqtt-tls; then
        d_tls=y
    fi
    ANS_MQTT_TLS_SAN=""
    if ask_yn MQTT_TLS "Включить MQTT по TLS (порт 8883)?" "$d_tls"; then
        ask MQTT_TLS_SAN "Имена и IP брокера для сертификата (через пробел)" "${CONF_MQTT_TLS_SAN:-$PU_HOST}" valid_san
    fi
    SECRET_MAX_BOT_TOKEN=""
    if [ ! -s "$SECRETS_DIR/max_bot_token" ]; then
        ask MAX_BOT_TOKEN "Токен бота Max (пусто — без Max)" "" valid_token
        SECRET_MAX_BOT_TOKEN=$ANS_MAX_BOT_TOKEN
    fi
    ask MAX_BOT_USERNAME "Имя бота Max (пусто — без Max)" "$(env_cur MAX_BOT_USERNAME "$mode")" valid_token
    d_daily=y
    if { [ "$mode" != install ] || [ "$CONF_STATE" = installing ]; } && ! has_profile "$CONF_PROFILES" backup; then
        d_daily=n
    fi
    ANS_BACKUP_RETENTION_DAYS=$(env_cur BACKUP_RETENTION_DAYS "$mode")
    if ask_yn BACKUP_DAILY "Ежедневные дампы БД в томе backup_data (профиль backup)?" "$d_daily"; then
        ask BACKUP_RETENTION_DAYS "Сколько дней хранить ежедневные дампы" "$ANS_BACKUP_RETENTION_DAYS" valid_days
    fi
    ask BACKUP_DIR "Каталог на хосте для дампов перед обновлением" "${OPT_BACKUP_DIR:-${CONF_BACKUP_DIR:-$DEFAULT_BACKUP_DIR}}" valid_abs_path
    d_mon=n
    if has_profile "$CONF_PROFILES" monitoring; then
        d_mon=y
    fi
    ask_yn MONITORING "Мониторинг Zabbix (профиль monitoring)?" "$d_mon" || true
    d_sms=n
    if has_profile "$CONF_PROFILES" sms; then
        d_sms=y
    fi
    SECRET_SMS_API_USER=""
    SECRET_SMS_API_PASSWORD=""
    if ask_yn SMS "SMS-шлюз Yeastar (WireGuard-сайдкар wg_gateway)?" "$d_sms"; then
        if [ ! -s "$SECRETS_DIR/sms_api_user" ]; then
            ask_line SMS_API_USER "Пользователь SMS API шлюза (пусто — задать позже в secrets/sms_api_user)" 0
            SECRET_SMS_API_USER=$ANS_SMS_API_USER
        fi
        if [ ! -s "$SECRETS_DIR/sms_api_password" ]; then
            ask_line SMS_API_PASSWORD "Пароль SMS API шлюза (пусто — задать позже в secrets/sms_api_password)" 1
            SECRET_SMS_API_PASSWORD=$ANS_SMS_API_PASSWORD
        fi
    fi
}

valid_line() { [[ $1 != *$'\n'* && $1 != *$'\r'* ]]; }

# Однострочное значение без ограничений на символы; пусто допустимо. Значение в сообщениях не выводится.
ask_line() {
    local var=$1 prompt=$2 hidden=$3 env="SCUD_$1" val=""
    if [[ -v $env ]]; then
        val=${!env}
        valid_line "$val" || die "$env: допустима одна строка без перевода строки"
    elif [ "$ASSUME_YES" != 1 ]; then
        while :; do
            printf '%s: ' "$prompt" >&2
            if [ "$hidden" = 1 ]; then
                IFS= read -rs val || val=""
                printf '\n' >&2
            else
                IFS= read -r val || val=""
            fi
            if valid_line "$val"; then
                break
            fi
            warn "значение должно быть одной строкой, повторите ввод"
        done
    fi
    printf -v "ANS_$var" '%s' "$val"
}

ask_admin_password() {
    local p1 p2
    if [[ -v SCUD_ADMIN_PASSWORD ]]; then
        valid_password "$SCUD_ADMIN_PASSWORD" || die "SCUD_ADMIN_PASSWORD: не короче 8 символов, без пробелов, не changeme/password"
        SECRET_ADMIN_PASSWORD=$SCUD_ADMIN_PASSWORD
        return 0
    fi
    if ask_yn ADMIN_PASSWORD_GENERATE "Сгенерировать пароль администратора?" y; then
        return 0
    fi
    while :; do
        printf 'Пароль администратора: ' >&2
        IFS= read -rs p1 || die "ввод пароля прерван"
        printf '\nПовторите пароль: ' >&2
        IFS= read -rs p2 || die "ввод пароля прерван"
        printf '\n' >&2
        if [ "$p1" != "$p2" ]; then
            warn "пароли не совпадают"
        elif ! valid_password "$p1"; then
            warn "пароль: не короче 8 символов, без пробелов, не changeme/password"
        else
            SECRET_ADMIN_PASSWORD=$p1
            return 0
        fi
    done
}

config_summary() {
    local admin="генерируется" daily=$ANS_BACKUP_DAILY
    if [ -n "${SECRET_ADMIN_PASSWORD:-}" ]; then
        admin="введён"
    elif [ -s "$SECRETS_DIR/admin_password" ] && [ "${FORCE_ADMIN_QUESTION:-0}" != 1 ]; then
        admin="из secrets/admin_password"
    fi
    if [ "$daily" = y ]; then
        daily="да, хранить $ANS_BACKUP_RETENTION_DAYS дн."
    else
        daily=нет
    fi
    info "Итог параметров"
    note "каталог:           $ROOT_DIR"
    note "адрес сайта:       $ANS_PUBLIC_URL (CORS $PU_ORIGIN, SECURE_COOKIES=$PU_SECURE)"
    note "доверенный прокси: $ANS_TRUSTED_PROXY_CIDR"
    note "режим:             $ANS_APP_ENV"
    note "администратор:     $ANS_ADMIN_PHONE, пароль $admin"
    note "MQTT по сети:      $(yn_ru "$ANS_MQTT_NETWORK"); TLS 8883: $(yn_ru "$ANS_MQTT_TLS")${ANS_MQTT_TLS_SAN:+ ($ANS_MQTT_TLS_SAN)}"
    note "бот Max:           ${ANS_MAX_BOT_USERNAME:-нет}"
    note "ежедневные дампы:  $daily"
    note "дампы на хосте:    $ANS_BACKUP_DIR"
    note "мониторинг Zabbix: $(yn_ru "$ANS_MONITORING")"
    note "SMS-шлюз Yeastar:  $(yn_ru "$ANS_SMS")"
}

yn_ru() {
    if [ "$1" = y ]; then echo да; else echo нет; fi
}

apply_config() {
    local mode=$1 profs="" bind=127.0.0.1 tls_bind=127.0.0.1 name
    parse_public_url "$ANS_PUBLIC_URL" || die "неверный адрес сайта"
    if [ "$ANS_BACKUP_DAILY" = y ]; then
        profs=$(profiles_with "$profs" backup)
    fi
    if [ "$ANS_MQTT_TLS" = y ]; then
        profs=$(profiles_with "$profs" mqtt-tls)
        tls_bind=0.0.0.0
    fi
    if [ "$ANS_MONITORING" = y ]; then
        profs=$(profiles_with "$profs" monitoring)
    fi
    if [ "$ANS_SMS" = y ]; then
        profs=$(profiles_with "$profs" sms)
    fi
    if [ "$ANS_MQTT_NETWORK" = y ]; then
        bind=0.0.0.0
    fi
    CONF_PROFILES=$profs
    CONF_PUBLIC_URL=$ANS_PUBLIC_URL
    CONF_BACKUP_DIR=$ANS_BACKUP_DIR
    CONF_MQTT_TLS_SAN=$ANS_MQTT_TLS_SAN
    info "Секреты"
    ensure_secrets || die "не удалось подготовить секреты"
    audit_secrets "$ANS_APP_ENV" || exit 1
    info "Файл .env"
    if ! {
        kv_set "$ENV_FILE" CORS_ALLOWED_ORIGIN "$PU_ORIGIN" &&
            kv_set "$ENV_FILE" SECURE_COOKIES "$PU_SECURE" &&
            kv_set "$ENV_FILE" TRUSTED_PROXY_CIDR "$ANS_TRUSTED_PROXY_CIDR" &&
            kv_set "$ENV_FILE" APP_ENV "$ANS_APP_ENV" &&
            kv_set "$ENV_FILE" ADMIN_PHONE "$ANS_ADMIN_PHONE" &&
            kv_set "$ENV_FILE" MQTT_BIND_ADDR "$bind" &&
            kv_set "$ENV_FILE" MQTT_TLS_BIND_ADDR "$tls_bind" &&
            kv_set "$ENV_FILE" MAX_BOT_USERNAME "$ANS_MAX_BOT_USERNAME" &&
            kv_set "$ENV_FILE" BACKUP_RETENTION_DAYS "$ANS_BACKUP_RETENTION_DAYS"
    }; then
        die "не удалось записать $ENV_FILE"
    fi
    if [ "$mode" = install ]; then
        for name in redis_password mqtt_password acm_db_password; do
            kv_set "$ENV_FILE" "${name^^}" "$(secret_read "$name")" || die "не удалось записать $ENV_FILE"
        done
    fi
    env_merge_defaults "$mode"
    gate_sms_profile
    check_redis_mirror
    ensure_tls_certs
}

mqtt_tls_cert_dir() {
    local dir
    dir=$(kv_get "$ENV_FILE" MQTT_TLS_CERT_DIR 2>/dev/null) || dir=""
    [ -n "$dir" ] || dir=./mqtt-tls/certs
    case "$dir" in
        /*) ;;
        *) dir=$BACKEND_DIR/${dir#./} ;;
    esac
    printf '%s' "${dir%/}"
}

# Генератор выпускает server.pem в <out>/certs и падает без ca.key, поэтому
# вызывается только при первой установке, когда сертификата ещё нет.
ensure_tls_certs() {
    local t dir out
    local -a args=() san=()
    has_profile "$CONF_PROFILES" mqtt-tls || return 0
    dir=$(mqtt_tls_cert_dir)
    if [ -s "$dir/server.pem" ]; then
        note "сертификат MQTT TLS уже есть: $dir/server.pem"
        return 0
    fi
    out=$BACKEND_DIR/mqtt-tls
    if [ "$(basename "$dir")" = certs ]; then
        out=$(dirname "$dir")
    fi
    args=(--out "$out")
    read -ra san <<<"${CONF_MQTT_TLS_SAN:-}"
    for t in ${san[@]+"${san[@]}"}; do
        if is_ipv4 "$t"; then
            args+=(--ip "$t")
        else
            args+=(--dns "$t")
        fi
    done
    info "Сертификаты MQTT TLS"
    bash "$BACKEND_DIR/scripts/gen-mqtt-tls-certs.sh" "${args[@]}" ||
        die "не удалось подготовить сертификаты MQTT TLS (backend/scripts/gen-mqtt-tls-certs.sh)"
    if [ "$out/certs" != "$dir" ]; then
        warn "MQTT_TLS_CERT_DIR=$dir: сертификат выпущен в $out/certs/server.pem — скопируйте его в $dir до запуска mqtt_tls"
    fi
}

# --- Проверки ---

offer_install_docker() {
    have apt-get || return 1
    if [ "$(id -u)" != 0 ]; then
        err "Docker не найден; для установки через apt запустите скрипт от root"
        return 1
    fi
    ask_yn INSTALL_DOCKER "Docker не найден. Установить docker.io и docker-compose-v2 через apt?" y || return 1
    if [ -f /etc/astra_version ] || grep -qi astra /etc/os-release 2>/dev/null; then
        note "Astra Linux: из /etc/apt/sources.list убирается несертифицированный репозиторий extended (копия: sources.list.deploy.bak)"
        sed -i.deploy.bak '/extended/d' /etc/apt/sources.list || return 1
    fi
    apt-get update && apt-get install -y docker.io docker-compose-v2 || return 1
    if have systemctl; then
        systemctl enable --now docker || true
    fi
}

port_busy() {
    local port=$1
    if have ss; then
        ss -ltn 2>/dev/null | awk 'NR > 1 { print $4 }' | grep -Eq "[:.]$port\$"
    elif have netstat; then
        netstat -ltn 2>/dev/null | awk 'NR > 2 { print $4 }' | grep -Eq "[:.]$port\$"
    else
        return 1
    fi
}

check_resources() {
    local path=$1 avail mem
    while [ ! -d "$path" ]; do
        path=$(dirname "$path")
    done
    avail=$(df -Pk "$path" 2>/dev/null | awk 'NR == 2 { print $4 }')
    if [ -n "$avail" ] && [ "$avail" -lt $((20 * 1024 * 1024)) ]; then
        warn "на разделе с $path свободно меньше 20 ГБ ($((avail / 1024 / 1024)) ГБ)"
    fi
    mem=$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo 2>/dev/null)
    if [ -n "$mem" ] && [ "$mem" -lt $((8 * 1024 * 1024 - 262144)) ]; then
        warn "оперативной памяти меньше 8 ГБ ($((mem / 1024)) МБ): сервисы могут не уместиться"
    fi
}

preflight() {
    local ver
    [ "$(uname -s)" = Linux ] || die "скрипт рассчитан на Linux-сервер"
    if [ "${BASH_VERSINFO[0]}" -lt 4 ]; then
        die "нужен bash 4 или новее"
    fi
    if [ "$(id -u)" != 0 ]; then
        warn "скрипт запущен не от root: права на каталоги дампов и секретов могут быть выставлены не полностью"
    fi
    if ! have docker; then
        offer_install_docker || die "нужен Docker 24+ и docker compose 2.20+ (Astra/Debian: apt install docker.io docker-compose-v2)"
    fi
    docker info >/dev/null 2>&1 ||
        die "Docker недоступен: запустите от root или добавьте пользователя в группу docker и проверьте, что служба docker запущена"
    ver=$(compose_version) || die "нет docker compose v2 (apt install docker-compose-v2 или docker-compose-plugin)"
    version_ge "$ver" "$MIN_COMPOSE_VERSION" || die "docker compose $ver слишком старый: нужен $MIN_COMPOSE_VERSION или новее (up --wait)"
    have curl || warn "нет curl: проверка /health после запуска будет пропущена"
}

json_escape() {
    local s=$1
    s=${s//\\/\\\\}
    s=${s//\"/\\\"}
    printf '%s' "$s"
}

smoke_health() {
    local i code=""
    have curl || return 0
    for ((i = 0; i < 30; i++)); do
        code=$(curl -s -o /dev/null -m 5 -w '%{http_code}' http://localhost/health || true)
        if [ "$code" = 200 ]; then
            note "gateway /health: 200"
            return 0
        fi
        sleep 2
    done
    err "gateway /health отвечает ${code:-нет ответа}"
    return 1
}

smoke_login() {
    local phone=$1 pw=$2 code body
    have curl || return 0
    body=$(printf '{"phone":"%s","password":"%s"}' "$(json_escape "$phone")" "$(json_escape "$pw")")
    code=$(printf '%s' "$body" | curl -s -o /dev/null -m 15 -w '%{http_code}' -X POST \
        -H 'Content-Type: application/json' -H 'Host: localhost' --data-binary @- \
        http://localhost/api/v1/auth/login || true)
    if [ "$code" = 200 ]; then
        note "вход администратора: 200"
        return 0
    fi
    err "вход администратора ($phone) вернул ${code:-нет ответа}, ожидался 200"
    return 1
}

acquire_lock() {
    if ! mkdir -p "$STATE_DIR" || ! chmod 0700 "$STATE_DIR"; then
        die "не удалось создать $STATE_DIR"
    fi
    have flock || return 0
    exec 9>"$STATE_DIR/lock"
    flock -n 9 || die "deploy.sh уже выполняется для $ROOT_DIR"
}

now_utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

# --- Команды ---

obtain_code_install() {
    local sr
    sr=$(self_root || true)
    if [ -n "$OPT_ARCHIVE" ]; then
        mkdir -p "$ROOT_DIR" || die "не удалось создать $ROOT_DIR"
        STAGE_BASE=$ROOT_DIR/.deploy/tmp
        extract_archive "$(abs_path "$OPT_ARCHIVE")"
        install_tree "$RELEASE_DIR" "$REL_SHA" || die "не удалось разложить файлы релиза"
        CONF_SOURCE=archive
        CONF_GIT_REF=$REL_REF
        CONF_VERSION=$REL_SHA
    elif [ -n "$OPT_GIT" ]; then
        have git || die "нужен git"
        CONF_SOURCE=git
        CONF_GIT_URL=$OPT_GIT
        if [ -d "$ROOT_DIR/.git" ]; then
            note "в $ROOT_DIR уже есть git-клон — используется он"
            if [ -n "$OPT_REF" ]; then
                git_update_code "$OPT_REF"
            fi
        else
            if [ -d "$ROOT_DIR" ] && [ -n "$(ls -A "$ROOT_DIR" 2>/dev/null)" ]; then
                die "каталог $ROOT_DIR не пуст; укажите другой --dir"
            fi
            info "git clone $OPT_GIT -> $ROOT_DIR"
            git clone -q "$OPT_GIT" "$ROOT_DIR" || die "git clone не удался"
            if [ -n "$OPT_REF" ]; then
                git_checkout_ref "$OPT_REF"
            fi
        fi
        CONF_GIT_REF=${OPT_REF:-$(git -C "$ROOT_DIR" symbolic-ref --short -q HEAD || git -C "$ROOT_DIR" rev-parse HEAD)}
        CONF_VERSION=$(git -C "$ROOT_DIR" rev-parse HEAD)
    elif [ -n "$sr" ] && [ "$sr" = "$ROOT_DIR" ]; then
        if [ -d "$ROOT_DIR/.git" ] && have git; then
            CONF_SOURCE=git
            CONF_GIT_URL=$(git -C "$ROOT_DIR" remote get-url origin 2>/dev/null || true)
            if [ -n "$OPT_REF" ]; then
                git_update_code "$OPT_REF"
            fi
            CONF_GIT_REF=${OPT_REF:-$(git -C "$ROOT_DIR" symbolic-ref --short -q HEAD || git -C "$ROOT_DIR" rev-parse HEAD)}
            CONF_VERSION=$(git -C "$ROOT_DIR" rev-parse HEAD)
        elif release_info "$ROOT_DIR"; then
            install_tree "$ROOT_DIR" "$REL_SHA" || die "не удалось зарегистрировать релиз"
            CONF_SOURCE=archive
            CONF_GIT_REF=$REL_REF
            CONF_VERSION=$REL_SHA
            IMAGES_TAR=""
            if [ -f "$ROOT_DIR/images.tar" ]; then
                IMAGES_TAR=$ROOT_DIR/images.tar
            fi
        else
            die "в $ROOT_DIR нет ни .git, ни VERSION: непонятно, какая это версия"
        fi
    elif [ -n "$sr" ] && release_info "$sr"; then
        info "Копирование релиза ${REL_REF:-?} (${REL_SHA:0:8}) в $ROOT_DIR"
        if [ -d "$ROOT_DIR" ] && [ -n "$(ls -A "$ROOT_DIR" 2>/dev/null)" ] && [ ! -f "$ROOT_DIR/VERSION" ]; then
            die "каталог $ROOT_DIR не пуст; укажите другой --dir"
        fi
        install_tree "$sr" "$REL_SHA" || die "не удалось разложить файлы релиза"
        CONF_SOURCE=archive
        CONF_GIT_REF=$REL_REF
        CONF_VERSION=$REL_SHA
        IMAGES_TAR=""
        if [ -f "$sr/images.tar" ]; then
            IMAGES_TAR=$sr/images.tar
        fi
    elif [ -n "$sr" ]; then
        die "скрипт запущен из рабочей копии $sr: установите в неё (--dir $sr) или укажите --git URL / --archive FILE"
    else
        die "укажите источник кода: --git URL или --archive FILE"
    fi
}

final_summary() {
    local pw
    pw=$(secret_read admin_password || true)
    printf '\n'
    info "Готово"
    note "сайт:           $CONF_PUBLIC_URL (локально: http://localhost/)"
    note "администратор:  $(kv_get "$ENV_FILE" ADMIN_PHONE || echo +79000000000)"
    note "пароль:         $pw"
    note "                (хранится в $SECRETS_DIR/admin_password; меняется через API после первого входа)"
    note "дампы на хосте: $CONF_BACKUP_DIR (deploy.sh backup)"
    note "версия:         $(tree_version_label)"
    info "Дальше"
    if ! parse_public_url "$CONF_PUBLIC_URL" || [ "$PU_SECURE" = false ]; then
        note "- поставьте TLS-терминатор (nginx) перед сервером: backend/DEPLOYMENT.md, раздел TLS"
    else
        note "- TLS-терминатор для $CONF_PUBLIC_URL настраивается отдельно: backend/DEPLOYMENT.md, раздел TLS"
    fi
    if has_profile "$CONF_PROFILES" mqtt-tls; then
        note "- CA для прошивки плат: $BACKEND_DIR/mqtt-tls/ca/ca.crt (ключ ca/ca.key храните офлайн)"
    fi
    if has_profile "$CONF_PROFILES" monitoring; then
        note "- Zabbix: http://127.0.0.1:8091 (Admin/zabbix — смените пароль), шаблоны: monitoring/zabbix/README.md"
    fi
    note "- копию $SECRETS_DIR и $ENV_FILE храните вне сервера"
}

install_finish() {
    build_images
    info "Запуск сервисов"
    compose_up --remove-orphans || exit 1
    info "Журнал блоков схемы"
    apply_schema_upgrades --baseline || exit 1
    info "Проверка"
    smoke_health || exit 1
    smoke_login "$(kv_get "$ENV_FILE" ADMIN_PHONE || echo +79000000000)" "$(secret_read admin_password)" || exit 1
    CONF_STATE=installed
    CONF_VERSION=$(tree_version || true)
    CONF_TARGET_VERSION=""
    CONF_UPDATED_AT=$(now_utc)
    save_conf
    final_summary
}

cmd_install() {
    local resume=0 vols
    preflight
    if [ -f "$CONF_FILE" ]; then
        load_conf
        if [ "$CONF_STATE" = installing ]; then
            resume=1
            warn "найдена незавершённая установка в $ROOT_DIR — продолжаю с сохранёнными параметрами"
        else
            die "в $ROOT_DIR уже есть установка: используйте update или reinstall"
        fi
    else
        reset_conf
    fi
    vols=$(existing_volumes)
    if [ -n "$vols" ] && [ "$resume" = 0 ]; then
        die "на сервере уже есть тома БД ($(printf '%s' "$vols" | tr '\n' ' ')): используйте update или reinstall"
    fi
    if [ "$resume" = 0 ]; then
        check_resources "$ROOT_DIR"
        if port_busy 80; then
            die "порт 80 занят другим процессом"
        fi
    fi
    obtain_code_install
    acquire_lock
    STAGE_BASE=$STATE_DIR/tmp
    warn_override
    if [ "$resume" = 1 ]; then
        ASSUME_YES=1 ask_config install
    else
        ask_config install
        config_summary
        confirm "Установить?"
    fi
    if [ "$resume" = 0 ] && [ "$ANS_MQTT_NETWORK" = y ] && port_busy 1883; then
        die "порт 1883 занят другим процессом"
    fi
    if [ "$resume" = 0 ] && [ "$ANS_MQTT_TLS" = y ] && port_busy 8883; then
        die "порт 8883 занят другим процессом"
    fi
    apply_config install
    CONF_STATE=installing
    CONF_INSTALLED_AT=${CONF_INSTALLED_AT:-$(now_utc)}
    save_conf
    install_finish
}

installed_script() {
    if [ -f "$BACKEND_DIR/scripts/deploy.sh" ]; then
        printf '%s' "$BACKEND_DIR/scripts/deploy.sh"
    else
        printf '%s' "$SELF_PATH"
    fi
}

maybe_reexec() {
    local new=$BACKEND_DIR/scripts/deploy.sh
    local -a args=(update --resume-after-fetch --dir "$ROOT_DIR" -y)
    [ -f "$new" ] || return 0
    if [ "$(sha256_stdin <"$new")" = "${SELF_SUM:-}" ]; then
        return 0
    fi
    if ! grep -q -- '--resume-after-fetch' "$new"; then
        warn "в новой версии нет deploy.sh с поддержкой продолжения — обновление завершает текущий скрипт"
        return 0
    fi
    info "Продолжение обновления новой версией deploy.sh"
    if [ "$NO_BUILD" = 1 ]; then
        args+=(--no-build)
    fi
    export SCUD_IMAGES_TAR=${IMAGES_TAR:-} SCUD_DUMP_DIR=${DUMP_DIR:-}
    SCUD_CLEANUP_DIRS=$(printf '%s\n' ${CLEANUP_DIRS[@]+"${CLEANUP_DIRS[@]}"})
    export SCUD_CLEANUP_DIRS
    CLEANUP_DIRS=()
    exec bash "$new" "${args[@]}"
}

cmd_update() {
    local pending new_version
    preflight
    load_install_state
    acquire_lock
    STAGE_BASE=$STATE_DIR/tmp
    if [ "$CONF_STATE" = installing ]; then
        die "установка в $ROOT_DIR не завершена: повторите deploy.sh install"
    fi
    warn_override
    if [ "$RESUME_AFTER_FETCH" = 1 ]; then
        IMAGES_TAR=${SCUD_IMAGES_TAR:-}
        DUMP_DIR=${SCUD_DUMP_DIR:-}
        local d
        while IFS= read -r d; do
            if [ -n "$d" ]; then
                CLEANUP_DIRS+=("$d")
            fi
        done <<<"${SCUD_CLEANUP_DIRS:-}"
    else
        if [ "$RECONFIGURE" = 1 ]; then
            ask_config update
            config_summary
        fi
        info "Обновление $ROOT_DIR"
        note "текущая версия: $(tree_version_label)"
        plan_code_update
        if [ "$CODE_KEEP" = 1 ]; then
            note "новый код: нет (новый архив не указан — переразвёртывание текущей версии)"
        elif [ -n "$OPT_ARCHIVE" ]; then
            note "новый код: архив $OPT_ARCHIVE"
        elif [ "$CONF_SOURCE" = git ]; then
            note "новый код: git ${OPT_REF:-$CONF_GIT_REF}"
        fi
        note "профили: ${CONF_PROFILES:-нет}; дамп перед обновлением: $([ "$SKIP_BACKUP" = 1 ] && echo 'пропускается (--skip-backup)' || echo "$CONF_BACKUP_DIR")"
        confirm "Обновить?"
        ensure_postgres_up
        floor_guard || exit 1
        if [ "$RECONFIGURE" = 1 ]; then
            apply_config update
            save_conf
        fi
        if [ "$SKIP_BACKUP" = 1 ]; then
            warn "дамп БД перед обновлением пропущен (--skip-backup)"
        else
            dump_databases pre-update || die "дамп не удался — обновление остановлено, данные и сервисы не изменены. Повторите или запустите с --skip-backup"
        fi
        # Повтор прерванного update: на месте текущих образов могут быть уже новые.
        if [ "$CONF_STATE" = updating ] && [ -n "$CONF_PREV_VERSION" ] && [ -s "$STATE_DIR/rollback-images" ]; then
            note "прошлое обновление не завершено: образы :rollback и версия для отката (${CONF_PREV_VERSION:0:12}) остаются прежними"
        else
            CONF_STATE=updating
            CONF_PREV_VERSION=""
            rm -f "$STATE_DIR/rollback-images"
            save_conf
            tag_rollback_images || die "не удалось пометить текущие образы для отката"
            CONF_PREV_VERSION=$CONF_VERSION
            save_conf
        fi
        fetch_code_update
        CONF_TARGET_VERSION=$(tree_version || true)
        save_conf
        maybe_reexec
    fi
    new_version=$(tree_version || true)
    ensure_secrets || die "не удалось подготовить секреты"
    env_merge_defaults update
    gate_sms_profile defer
    if [ "${#ENV_ADDED[@]}" -gt 0 ]; then
        note "в .env добавлены ключи (со значениями, которые и так действовали): ${ENV_ADDED[*]}"
    fi
    check_redis_mirror
    audit_secrets "$(kv_get "$ENV_FILE" APP_ENV || echo development)" || exit 1
    warn_insecure_env
    ensure_tls_certs
    build_images
    pending=$(pending_commands_count)
    local self
    self=$(installed_script)
    FAIL_HINT="Обновление прервано после остановки сервисов. Варианты:
  - устранить причину и повторить: $self update --dir $ROOT_DIR
  - вернуть прежнюю версию: $self rollback --dir $ROOT_DIR${DUMP_DIR:+
  - дамп БД до обновления: $DUMP_DIR}"
    info "Остановка API и шлюза на время изменения схемы"
    # Работающий шлюз без API через ~90 с становится unhealthy, и up --wait падает, не дождавшись восстановления.
    dc stop apache_gateway "${APP_SERVICES[@]}" || die "не удалось остановить сервисы"
    restart_postgres_user || exit 1
    apply_schema_upgrades || exit 1
    sync_acm_role || exit 1
    info "Запуск history_api"
    compose_up history_api || exit 1
    stop_disabled_profile_services
    info "Запуск всех сервисов"
    compose_up --remove-orphans || exit 1
    smoke_health || exit 1
    FAIL_HINT=""
    CONF_VERSION=$new_version
    CONF_TARGET_VERSION=""
    CONF_UPDATED_AT=$(now_utc)
    CONF_STATE=installed
    if [ "$SMS_DEFERRED" = 1 ]; then
        CONF_PROFILES=$(profiles_with "$CONF_PROFILES" sms)
    fi
    save_conf
    printf '\n'
    info "Обновление завершено: $(tree_version_label)"
    if [ -n "$pending" ] && [ "$pending" != 0 ]; then
        warn "$pending незавершённых команд старше 2 минут переведены в failed (истёк срок ожидания ответа шкафа)."
    fi
    note "шкафы показываются offline до своего следующего stat (до ~5 минут)"
    note "откат к прежней версии: $self rollback --dir $ROOT_DIR"
    if [ -n "${DUMP_DIR:-}" ]; then
        note "дамп БД до обновления: $DUMP_DIR"
    fi
}

cmd_reinstall() {
    local word="" keep_secrets=y keep_config=y update_code=n ts name old=""
    preflight
    load_install_state
    acquire_lock
    STAGE_BASE=$STATE_DIR/tmp
    warn "reinstall удалит ВСЕ данные установки: тома БД, redis, backup_data, mqtt_config"
    warn "(пользователи плат relay-* в mqtt_config пропадут — платы придётся провизионить заново)."
    if [[ -v SCUD_CONFIRM_WIPE ]]; then
        word=$SCUD_CONFIRM_WIPE
    elif [ "$ASSUME_YES" = 0 ]; then
        printf 'Для подтверждения введите УДАЛИТЬ: ' >&2
        IFS= read -r word || word=""
    fi
    [ "$word" = УДАЛИТЬ ] || die "переустановка не подтверждена (без вопросов: SCUD_CONFIRM_WIPE=УДАЛИТЬ)"
    if [ "$SKIP_BACKUP" = 1 ]; then
        warn "дамп БД пропущен (--skip-backup)"
    elif [ -n "$(existing_volumes)" ]; then
        ensure_postgres_up
        dump_databases pre-reinstall || die "дамп не удался — переустановка остановлена, данные не изменены"
    fi
    if ! ask_yn KEEP_SECRETS "Сохранить текущие секреты (пароли БД, ключи; без них дампы восстанавливаются так же)?" y; then
        keep_secrets=n
    fi
    if ! ask_yn KEEP_CONFIG "Сохранить текущие параметры (.env, deploy.conf)?" y; then
        keep_config=n
    fi
    if [ -n "$OPT_REF" ] || [ -n "$OPT_ARCHIVE" ]; then
        update_code=y
    fi
    if ask_yn UPDATE_CODE "Обновить код перед установкой?" "$update_code"; then
        update_code=y
    else
        update_code=n
    fi
    FORCE_ADMIN_QUESTION=0
    if [ "$keep_secrets" = n ]; then
        FORCE_ADMIN_QUESTION=1
    fi
    if [ "$keep_config" = n ]; then
        ask_config install
        config_summary
        confirm "Переустановить с этими параметрами?"
    elif [ "$keep_secrets" = n ]; then
        SECRET_ADMIN_PASSWORD=""
        ask_admin_password
    fi
    if [ "$update_code" = y ]; then
        fetch_code_update
    fi
    info "Остановка и удаление контейнеров и томов"
    dc_all down -v --remove-orphans || die "docker compose down не удался"
    if [ "$keep_secrets" = n ] && [ -d "$SECRETS_DIR" ]; then
        ts=$(date -u +%Y%m%dT%H%M%SZ)
        old=$BACKEND_DIR/secrets.old-$ts
        mv "$SECRETS_DIR" "$old" || die "не удалось отложить старые секреты"
        chmod 0700 "$old"
        mkdir -m 0700 "$SECRETS_DIR" || die "не удалось создать $SECRETS_DIR"
        for name in "${EXTERNAL_SECRETS[@]}"; do
            if [ -s "$old/$name" ]; then
                cp -p "$old/$name" "$SECRETS_DIR/$name"
            fi
        done
        note "старые секреты: $old (выданные снаружи — токен Max, SMS API, ключи WireGuard — перенесены)"
    fi
    if [ "$keep_config" = n ]; then
        apply_config install
    else
        ensure_secrets || die "не удалось подготовить секреты"
        for name in redis_password mqtt_password acm_db_password; do
            kv_set "$ENV_FILE" "${name^^}" "$(secret_read "$name")" || die "не удалось записать $ENV_FILE"
        done
        env_merge_defaults install
        gate_sms_profile
        audit_secrets "$(kv_get "$ENV_FILE" APP_ENV || echo development)" || exit 1
        ensure_tls_certs
    fi
    CONF_STATE=installing
    CONF_PREV_VERSION=""
    CONF_TARGET_VERSION=""
    rm -f "$STATE_DIR/rollback-images"
    save_conf
    install_finish
}

cmd_rollback() {
    local prev rel
    preflight
    load_install_state
    acquire_lock
    STAGE_BASE=$STATE_DIR/tmp
    prev=$CONF_PREV_VERSION
    if [ -z "$prev" ] || [ ! -s "$STATE_DIR/rollback-images" ]; then
        die "нет сохранённой предыдущей версии: откат доступен один раз после успешного update"
    fi
    info "Откат $ROOT_DIR"
    note "текущая версия: $(tree_version_label)"
    note "вернётся:       ${prev:0:12} (код и образы с тегом :rollback); БД не изменяется"
    confirm "Откатить?"
    if [ "$CONF_SOURCE" = git ]; then
        if git_dirty "$ROOT_DIR"; then
            die "в $ROOT_DIR изменены отслеживаемые git файлы — откат кода невозможен"
        fi
        git -C "$ROOT_DIR" checkout -q --detach "$prev" || die "git checkout $prev не удался"
    else
        rel=$STATE_DIR/releases/$prev.tar.gz
        [ -f "$rel" ] || die "нет сохранённого релиза $rel"
        mkdir -p "$STAGE_BASE"
        RELEASE_DIR=$(mktemp -d "$STAGE_BASE/rollback.XXXXXX") || die "не удалось создать временный каталог"
        CLEANUP_DIRS+=("$RELEASE_DIR")
        tar -xzf "$rel" -C "$RELEASE_DIR" || die "релиз $rel повреждён"
        install_tree "$RELEASE_DIR" "$prev" || die "не удалось вернуть файлы релиза"
        if release_info "$ROOT_DIR"; then
            CONF_GIT_REF=$REL_REF
        fi
    fi
    restore_rollback_images || die "не удалось вернуть образы :rollback"
    info "Запуск сервисов"
    compose_up --remove-orphans || exit 1
    smoke_health || warn "проверка /health не прошла"
    CONF_VERSION=$prev
    CONF_PREV_VERSION=""
    CONF_TARGET_VERSION=""
    CONF_STATE=installed
    CONF_UPDATED_AT=$(now_utc)
    rm -f "$STATE_DIR/rollback-images"
    save_conf
    info "Откат завершён: $(tree_version_label)"
    note "Схема БД не откатывается: блоки аддитивны, прежние образы новые колонки игнорируют."
    note "Если нужны данные на момент до обновления — восстановите их из дампа:"
    print_restore_hint "$(latest_dump_dir)"
}

latest_dump_dir() {
    local d last=""
    for d in "$CONF_BACKUP_DIR"/*-pre-update; do
        if [ -d "$d" ]; then
            last=$d
        fi
    done
    printf '%s' "$last"
}

cmd_backup() {
    preflight
    load_install_state
    acquire_lock
    ensure_postgres_up
    dump_databases manual || die "дамп не удался"
    print_restore_hint "$DUMP_DIR"
}

cmd_migrate() {
    preflight
    load_install_state
    acquire_lock
    ensure_postgres_up
    if [ "$BASELINE" = 1 ]; then
        confirm "Отметить все блоки схемы как применённые, не выполняя их?"
        apply_schema_upgrades --baseline || exit 1
    else
        apply_schema_upgrades || exit 1
        sync_acm_role || exit 1
    fi
}

cmd_status() {
    local state entry svc db rel req file journal line dir
    state=$(install_state)
    info "Установка: $ROOT_DIR"
    if [ "$state" = none ]; then
        note "не найдена (нет backend/deploy.conf, .env и секретов)"
        return 0
    fi
    if [ "$state" = conf ]; then
        load_conf
    else
        infer_legacy_conf 1
        note "deploy.conf нет — параметры определены автоматически"
    fi
    [ -n "$CONF_BACKUP_DIR" ] || CONF_BACKUP_DIR=$DEFAULT_BACKUP_DIR
    note "источник:      $CONF_SOURCE${CONF_GIT_URL:+ $CONF_GIT_URL}${CONF_GIT_REF:+ (ref $CONF_GIT_REF)}"
    note "версия кода:   $(tree_version_label || true)"
    note "установлена:   ${CONF_VERSION:0:12}${CONF_PREV_VERSION:+, откат на ${CONF_PREV_VERSION:0:12}}${CONF_TARGET_VERSION:+, незавершённое обновление на ${CONF_TARGET_VERSION:0:12}}"
    note "состояние:     ${CONF_STATE:-?}; установлено ${CONF_INSTALLED_AT:-?}, обновлено ${CONF_UPDATED_AT:-?}"
    note "адрес сайта:   ${CONF_PUBLIC_URL:-?}"
    note "профили:       ${CONF_PROFILES:-нет}"
    dir=""
    for line in "$CONF_BACKUP_DIR"/*/; do
        if [ -d "$line" ]; then
            dir=${line%/}
        fi
    done
    note "последний дамп на хосте: ${dir:-нет} (каталог $CONF_BACKUP_DIR)"
    if [ -f "$BACKEND_DIR/docker-compose.override.yaml" ]; then
        note "применяется docker-compose.override.yaml"
    fi
    if ! docker_ok; then
        warn "Docker недоступен — состояние сервисов и журнал схемы не показаны"
        info "Блоки схемы в файлах"
        for entry in "${DB_MAP[@]}"; do
            IFS='|' read -r svc db rel req <<<"$entry"
            note "$db: $(schema_block_names "$BACKEND_DIR/$rel" 2>/dev/null | tr '\n' ' ' || true)"
        done
        return 0
    fi
    info "Сервисы"
    {
        printf 'SERVICE\tSTATE\tHEALTH\tIMAGE\n'
        dc ps -a --format '{{.Service}}\t{{.State}}\t{{.Health}}\t{{.Image}}' || true
    } | awk -F '\t' '{ printf "%-24s %-10s %-10s %s\n", $1, $2, ($3 == "" ? "-" : $3), $4 }'
    info "Блоки схемы"
    for entry in "${DB_MAP[@]}"; do
        IFS='|' read -r svc db rel req <<<"$entry"
        file=$BACKEND_DIR/$rel
        if ! journal=$(printf '%s\n' "SELECT name || ' ' || checksum FROM deploy_schema_upgrades;" | db_psql "$svc" "$db" -tA 2>/dev/null); then
            journal=""
            note "$db: журнал недоступен (БД остановлена или журнала ещё нет)"
        fi
        while IFS= read -r line; do
            [ -n "$line" ] && note "$db: $line"
        done < <(schema_status "$file" "$journal")
    done
    if line=$(dc_p "$(profiles_with "$CONF_PROFILES" backup)" exec -T backup cat /backups/last_run_status 2>/dev/null); then
        note "ежедневный бэкап: $line"
    fi
}

cmd_menu() {
    local def state
    state=$(install_state)
    if [ "$state" = none ]; then
        def=7
    else
        def=1
    fi
    if [ "$ASSUME_YES" = 1 ] && [[ ! -v SCUD_MENU ]]; then
        if [ "$def" = 7 ]; then CMD=install; else CMD=update; fi
        return 0
    fi
    info "СКУД: $ROOT_DIR ($([ "$state" = none ] && echo 'установка не найдена' || echo 'установка найдена'))"
    note "1) update     обновить с сохранением данных"
    note "2) status     состояние"
    note "3) backup     дамп всех БД"
    note "4) migrate    применить блоки схемы"
    note "5) rollback   вернуть предыдущую версию"
    note "6) reinstall  переустановить с нуля (данные будут удалены)"
    note "7) install    новая установка"
    note "0) выход"
    ask MENU "Действие" "$def" valid_menu
    case "$ANS_MENU" in
        1) CMD=update ;;
        2) CMD=status ;;
        3) CMD=backup ;;
        4) CMD=migrate ;;
        5) CMD=rollback ;;
        6) CMD=reinstall ;;
        7) CMD=install ;;
        0) exit 0 ;;
    esac
}

valid_menu() { [[ $1 =~ ^[0-7]$ ]]; }

# --- pack ---

pack_images() {
    local root=$1 sha=$2 out=$3 base wt rc=0 img
    local -a prof=(--profile backup --profile mqtt-tls --profile monitoring --profile sms) imgs=()
    docker_ok || {
        err "--with-images: Docker недоступен"
        return 1
    }
    base=$(mktemp -d) || return 1
    wt=$base/src
    git -C "$root" worktree add -q --detach "$wt" "$sha" || {
        rm -rf "$base"
        return 1
    }
    info "Сборка образов ревизии ${sha:0:8} (теги $COMPOSE_PROJECT-* на этой машине будут перезаписаны)"
    (cd "$wt/backend" && docker compose -p "$COMPOSE_PROJECT" "${prof[@]}" build) || rc=1
    if [ "$rc" = 0 ]; then
        mapfile -t imgs < <(cd "$wt/backend" && docker compose -p "$COMPOSE_PROJECT" "${prof[@]}" config --images | LC_ALL=C sort -u)
        for img in "${imgs[@]}"; do
            if ! docker image inspect "$img" >/dev/null 2>&1; then
                docker pull "$img" || rc=1
            fi
        done
    fi
    if [ "$rc" = 0 ] && [ "${#imgs[@]}" -gt 0 ]; then
        info "docker save (${#imgs[@]} образов)"
        docker save -o "$out" "${imgs[@]}" || rc=1
    fi
    git -C "$root" worktree remove --force "$wt" || true
    rm -rf "$base"
    return "$rc"
}

pack_release() {
    local root=$1 ref=$2 out=$3 with_images=$4 sha head label safe name stage
    git -C "$root" rev-parse --git-dir >/dev/null 2>&1 || {
        err "$root — не git-репозиторий; pack работает только из рабочей копии"
        return 1
    }
    sha=$(git -C "$root" rev-parse -q --verify "$ref^{commit}") || {
        err "ревизия '$ref' не найдена"
        return 1
    }
    head=$(git -C "$root" rev-parse -q --verify 'HEAD^{commit}' || true)
    if [ "$sha" = "$head" ] && git_dirty "$root"; then
        err "в рабочей копии есть незакоммиченные изменения — в архив они не попадут. Закоммитьте их или укажите --ref"
        return 1
    fi
    label=$ref
    if [ "$ref" = HEAD ]; then
        label=$(git -C "$root" rev-parse --abbrev-ref HEAD 2>/dev/null || echo HEAD)
    fi
    safe=$(printf '%s' "$label" | tr -c 'A-Za-z0-9._-' '-')
    name=scud-$safe-${sha:0:8}
    [ -n "$out" ] || out=$PWD/$name.tar.gz
    out=$(abs_path "$out")
    stage=$(mktemp -d) || return 1
    info "Архив ревизии $label (${sha:0:8})"
    if ! git -C "$root" archive --format=tar --prefix="$name/" "$sha" | tar -xf - -C "$stage"; then
        rm -rf "$stage"
        err "git archive не удался"
        return 1
    fi
    rm -f "$stage/pax_global_header"
    if [ ! -f "$stage/$name/backend/docker-compose.yaml" ]; then
        rm -rf "$stage"
        err "в ревизии $label нет backend/docker-compose.yaml"
        return 1
    fi
    {
        printf 'REF=%s\n' "$(kv_quote "$label" || echo "${sha:0:8}")"
        printf 'SHA=%s\n' "$sha"
        printf 'DATE=%s\n' "$(now_utc)"
    } >"$stage/$name/VERSION"
    if [ -f "$stage/$name/backend/scripts/deploy.sh" ]; then
        cp -p "$stage/$name/backend/scripts/deploy.sh" "$stage/$name/deploy.sh"
        chmod 0755 "$stage/$name/deploy.sh"
    fi
    if [ "$with_images" = 1 ]; then
        pack_images "$root" "$sha" "$stage/$name/images.tar" || {
            rm -rf "$stage"
            err "не удалось подготовить образы"
            return 1
        }
    fi
    if ! tar -C "$stage" -czf "$out" "$name"; then
        rm -rf "$stage"
        err "не удалось записать $out"
        return 1
    fi
    rm -rf "$stage"
    info "Готово: $out"
    note "sha256: $(sha256_stdin <"$out")"
    note "на сервере: tar -xzf $(basename "$out") && sudo ./$name/deploy.sh install   (или update)"
}

cmd_pack() {
    local root
    have git || die "нужен git"
    root=$(self_root || true)
    if [ -z "$root" ] || ! git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
        root=$(git rev-parse --show-toplevel 2>/dev/null) || die "pack запускается из git-репозитория СКУД"
    fi
    pack_release "$root" "${OPT_REF:-HEAD}" "$OPT_OUT" "$WITH_IMAGES" || exit 1
}

# --- main ---

on_exit() {
    local rc=$? d
    for d in ${CLEANUP_DIRS[@]+"${CLEANUP_DIRS[@]}"}; do
        rm -rf -- "$d"
    done
    if [ "$rc" -ne 0 ] && [ -n "${FAIL_HINT:-}" ]; then
        printf '\n%s\n' "$FAIL_HINT" >&2
    fi
}

main() {
    set -euo pipefail
    umask 022
    CLEANUP_DIRS=()
    FAIL_HINT=""
    IMAGES_TAR=""
    DUMP_DIR=""
    STAGE_BASE=${TMPDIR:-/tmp}
    SECRET_ADMIN_PASSWORD=""
    SECRET_MAX_BOT_TOKEN=""
    SECRET_SMS_API_USER=""
    SECRET_SMS_API_PASSWORD=""
    reset_conf
    parse_args "$@" || exit 2
    if [ "$SHOW_HELP" = 1 ]; then
        usage
        exit 0
    fi
    SELF_PATH=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")
    # Обновление кода может заменить этот самый файл: сравнивать надо с тем, что исполняется.
    SELF_SUM=$(sha256_stdin <"$SELF_PATH")
    trap on_exit EXIT
    if [ "$CMD" = pack ]; then
        cmd_pack
        exit 0
    fi
    resolve_dir
    if [ -z "$CMD" ]; then
        cmd_menu
        resolve_dir
    fi
    case "$CMD" in
        install) cmd_install ;;
        update) cmd_update ;;
        reinstall) cmd_reinstall ;;
        rollback) cmd_rollback ;;
        backup) cmd_backup ;;
        migrate) cmd_migrate ;;
        status) cmd_status ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then main "$@"; fi
