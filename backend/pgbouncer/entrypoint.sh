#!/bin/sh
set -eu

# PgBouncer transaction-pooler entrypoint (Variant B userlist generation).
#
# Делает три вещи и передаёт управление pgbouncer:
#   1. Читает пароль(и) ролей из смонтированных Docker-секретов (env-fallback)
#      и пишет /etc/pgbouncer/userlist.txt (plaintext-литералы; pgbouncer в
#      режиме scram-sha-256 считает SCRAM на лету). Файл 0600 — пароли НЕ
#      логируются ни при каких условиях.
#   2. Подставляет ${PGB_*} в ini-шаблон через envsubst.
#   3. exec pgbouncer /etc/pgbouncer/pgbouncer.ini (listen 0.0.0.0:6432).
#
# Конфигурируется через env (значения проставляет docker-compose):
#   PGB_DB_HOST              — хост postgres-инстанса (postgres_<svc>)
#   PGB_DB_NAME              — имя БД (scud_<db>)
#   PGB_DB_USER              — основная роль входа (default admin)
#   PGB_DB_PASSWORD_SECRET   — имя файла секрета пароля основной роли
#                              (default postgres_<svc>_password в /run/secrets)
#   PGB_EXTRA_USER           — доп. роль (для user-пулера: acm_ro). Необязат.
#   PGB_EXTRA_PASSWORD_SECRET— имя файла секрета пароля доп. роли (acm_db_password)
#   PGB_DEFAULT_POOL_SIZE / PGB_RESERVE_POOL_SIZE / PGB_MAX_CLIENT_CONN — sizing
#   PGB_ADMIN_PASSWORD_SECRET— имя файла секрета pgb_admin_password (монтируется
#                              и pgbouncer-entrypoint, и zabbix_agent2 — один
#                              секрет, два читателя)

TEMPLATE=/opt/pgbouncer/pgbouncer.ini.template
INI=/etc/pgbouncer/pgbouncer.ini
USERLIST=/etc/pgbouncer/userlist.txt

PGB_DB_USER="${PGB_DB_USER:-admin}"
PGB_DB_PASSWORD_SECRET="${PGB_DB_PASSWORD_SECRET:-}"
PGB_EXTRA_USER="${PGB_EXTRA_USER:-}"
PGB_EXTRA_PASSWORD_SECRET="${PGB_EXTRA_PASSWORD_SECRET:-}"
PGB_ADMIN_PASSWORD_SECRET="${PGB_ADMIN_PASSWORD_SECRET:-}"

# read_password <secret_file_name> <env_fallback_value>
# Приоритет: /run/secrets/<name> → значение env-fallback. Без trailing newline.
read_password() {
    _name="$1"
    _fallback="$2"
    if [ -n "$_name" ] && [ -s "/run/secrets/$_name" ]; then
        tr -d '\r\n' < "/run/secrets/$_name"
        return
    fi
    printf '%s' "$_fallback"
}

# Основная роль (admin postgres-инстанса). Env-fallback POSTGRES_PASSWORD
# совпадает с dev-default содержимым секрета postgres_<svc>_password.
MAIN_PW="$(read_password "$PGB_DB_PASSWORD_SECRET" "${POSTGRES_PASSWORD:-password}")"

# Пишем userlist atomically (tmp → mv) с правами 0600 ещё до записи паролей.
TMP="$USERLIST.tmp.$$"
( umask 0077; : > "$TMP" )
printf '"%s" "%s"\n' "$PGB_DB_USER" "$MAIN_PW" >> "$TMP"

# Доп. роль (только user-пулер: acm_ro ходит в scud_users). Env-fallback
# ACM_DB_PASSWORD совпадает с dev-default секрета acm_db_password.
if [ -n "$PGB_EXTRA_USER" ]; then
    EXTRA_PW="$(read_password "$PGB_EXTRA_PASSWORD_SECRET" "${ACM_DB_PASSWORD:-acm_ro_dev_password}")"
    printf '"%s" "%s"\n' "$PGB_EXTRA_USER" "$EXTRA_PW" >> "$TMP"
fi

# Локальная admin/stats-роль пулера для SHOW POOLS / SHOW STATS. Пароль —
# из смонтированного секрета pgb_admin_password (его же монтирует
# zabbix_agent2 — один секрет, два читателя).
ADMIN_PW="$(read_password "$PGB_ADMIN_PASSWORD_SECRET" "${PGB_ADMIN_PASSWORD:-}")"
printf '"%s" "%s"\n' "pgb_admin" "$ADMIN_PW" >> "$TMP"

mv "$TMP" "$USERLIST"
chmod 0600 "$USERLIST"

# Подстановка только наших переменных (иначе envsubst съел бы $-литералы из
# случайных мест шаблона). Экспортируем дефолты для sizing, если не заданы.
export PGB_DB_HOST PGB_DB_NAME
export PGB_DEFAULT_POOL_SIZE="${PGB_DEFAULT_POOL_SIZE:-20}"
export PGB_RESERVE_POOL_SIZE="${PGB_RESERVE_POOL_SIZE:-5}"
export PGB_MAX_CLIENT_CONN="${PGB_MAX_CLIENT_CONN:-200}"

# shellcheck disable=SC2016
# Single quotes НАМЕРЕННО: аргумент envsubst — это СПИСОК имён переменных в
# синтаксисе самого envsubst (он подставит только их), а не shell-expansion.
# Расширь мы их двойными кавычками — envsubst получил бы уже подставленные
# значения и заменил бы в шаблоне всё подряд.
envsubst '${PGB_DB_HOST} ${PGB_DB_NAME} ${PGB_DEFAULT_POOL_SIZE} ${PGB_RESERVE_POOL_SIZE} ${PGB_MAX_CLIENT_CONN}' \
    < "$TEMPLATE" > "$INI"

echo "pgbouncer: pooling ${PGB_DB_NAME} via ${PGB_DB_HOST}:5432 (transaction mode, pool=${PGB_DEFAULT_POOL_SIZE})"

# PgBouncer ОТКАЗЫВАЕТСЯ работать под root ("FATAL PgBouncer should not run as
# root"). Entrypoint исполняется от root (нужно для записи userlist 0600), а сам
# демон роняем под непривилегированного пользователя postgres (uid 100, есть в
# базовом образе). Передаём владение сгенерированными файлами и рантайм-каталогами
# postgres-пользователю, затем exec через runuser (сохраняет PID 1 → корректная
# доставка сигналов и graceful drain транзакций при SIGTERM).
PGB_RUN_USER="${PGB_RUN_USER:-postgres}"
chown "$PGB_RUN_USER" "$USERLIST" "$INI" /var/run/pgbouncer /var/log/pgbouncer
exec runuser -u "$PGB_RUN_USER" -- pgbouncer "$INI"
