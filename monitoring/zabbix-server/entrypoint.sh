#!/bin/sh
set -e

# DB_PASSWORD_FILE -> read into zabbix_server.conf DBPassword (Docker secret).
if [ -n "${DB_PASSWORD_FILE:-}" ] && [ -s "${DB_PASSWORD_FILE}" ]; then
  DB_PASSWORD="$(cat "${DB_PASSWORD_FILE}")"
fi

CONF=/etc/zabbix/zabbix_server.conf
{
  echo "DBHost=${DB_SERVER_HOST:-postgres_zabbix}"
  echo "DBName=${DB_NAME:-zabbix}"
  echo "DBUser=${DB_USER:-zabbix}"
  echo "DBPassword=${DB_PASSWORD:-}"
  echo "ListenPort=${ZBX_LISTENPORT:-10051}"
  echo "LogType=console"
} > "$CONF"

# ---------------------------------------------------------------------------
# Схема БД: загружается один раз при первом старте, затем пропускается.
# Файлы *.sql.gz живут в этом образе (пакет zabbix-server-pgsql), поэтому
# загрузку нельзя делать в init-скрипте postgres_zabbix.
# Порядок: schema -> images -> data (требование Zabbix).
# Идемпотентность: проверяем наличие таблицы `users` в схеме zabbix.
# ---------------------------------------------------------------------------
ZBX_DB_HOST="${DB_SERVER_HOST:-postgres_zabbix}"
ZBX_DB_NAME="${DB_NAME:-zabbix}"
ZBX_DB_USER="${DB_USER:-zabbix}"
SCHEMA_DIR="/usr/share/zabbix-server-pgsql"

echo "Waiting for postgres at ${ZBX_DB_HOST}:5432..."
# Ждём до 60 секунд (30 попыток × 2 с). pg_isready встроен через
# postgresql-client-15, установленный в Dockerfile.
i=0
until pg_isready -h "${ZBX_DB_HOST}" -p 5432 -U "${ZBX_DB_USER}" -d "${ZBX_DB_NAME}" -q; do
  i=$((i + 1))
  if [ "$i" -ge 30 ]; then
    echo "postgres_zabbix not ready after 60 s — abort" >&2
    exit 1
  fi
  sleep 2
done
echo "postgres_zabbix is ready."

# Проверяем, загружена ли схема (таблица users создаётся schema.sql.gz).
SCHEMA_LOADED=$(PGPASSWORD="${DB_PASSWORD:-}" psql \
  -h "${ZBX_DB_HOST}" -U "${ZBX_DB_USER}" -d "${ZBX_DB_NAME}" \
  -tAc "SELECT 1 FROM pg_class WHERE relname='users' AND relkind='r'" 2>/dev/null || true)

if [ "${SCHEMA_LOADED}" = "1" ]; then
  echo "Zabbix schema already loaded — skipping."
else
  echo "Loading Zabbix schema (schema -> images -> data)..."
  for part in schema images data; do
    echo "  Loading ${part}.sql.gz ..."
    zcat "${SCHEMA_DIR}/${part}.sql.gz" \
      | PGPASSWORD="${DB_PASSWORD:-}" psql \
          -v ON_ERROR_STOP=1 \
          -h "${ZBX_DB_HOST}" \
          -U "${ZBX_DB_USER}" \
          -d "${ZBX_DB_NAME}" \
          -q
  done
  echo "Zabbix schema loaded successfully."
fi

exec /usr/sbin/zabbix_server -f -c "$CONF"
