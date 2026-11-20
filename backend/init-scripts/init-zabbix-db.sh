#!/bin/bash
set -e

echo "Initializing Zabbix database role and schema..."

if [ ! -s /run/secrets/zabbix_db_password ]; then
    echo "zabbix_db_password secret is missing or empty" >&2
    exit 1
fi
ZBX_PASSWORD="$(cat /run/secrets/zabbix_db_password)"

# CREATE LOGIN-роль zabbix — owner схемы.
# Пароль НЕ конкатенируется в SQL: роль создаётся в DO-блоке без пароля,
# ALTER ROLE ... PASSWORD :'zbx_pw' подставляет его как безопасно-квотированный
# литерал (паттерн init-user-db.sh, строки 28-37).
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v zbx_pw="$ZBX_PASSWORD" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'zabbix') THEN
    CREATE ROLE zabbix LOGIN;
  END IF;
END
$$;
ALTER ROLE zabbix PASSWORD :'zbx_pw';
SQL

# CREATE DATABASE zabbix OWNER zabbix.
# CREATE DATABASE нельзя выполнить внутри транзакции/DO-блока, поэтому
# идемпотентность обеспечивается проверкой pg_database перед вызовом.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -tc "SELECT 1 FROM pg_database WHERE datname = 'zabbix'" \
    | grep -q 1 \
    || psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
         -c "CREATE DATABASE zabbix OWNER zabbix;"

# PostgreSQL 15 revoked the default CREATE privilege on the public schema.
# Without this grant, the zabbix role (non-superuser) gets
# «permission denied for schema public» when loading schema.sql.gz.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "zabbix" \
    -c "GRANT CREATE ON SCHEMA public TO zabbix;"

# Схема не загружается здесь: файлы *.sql.gz находятся в образе zabbix_server
# (пакет zabbix-server-pgsql), а не в образе postgres_zabbix.
# Загрузку schema -> images -> data выполняет entrypoint zabbix_server после
# старта и ожидания доступности БД (см. monitoring/zabbix-server/entrypoint.sh).

echo "Zabbix role and database ready."
