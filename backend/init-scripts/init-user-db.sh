#!/bin/bash
set -e

echo "Starting user_api database migrations..."

# SKIP_MIGRATIONS=1: повторный запуск на существующей БД только для синхронизации acm_ro.
[ "${SKIP_MIGRATIONS:-0}" = 1 ] || psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /migrations/001_init.sql

echo "User API migrations completed successfully."

# SEC-24: least-privilege роль для ACM.
# ACM — read-only клиент scud_users (читает роли/Допуски для проверки прав,
# никогда не пишет). Раньше он ходил под суперпользователем admin, что нарушает
# принцип минимальных привилегий. Создаём отдельную LOGIN-роль acm_ro только с
# правом SELECT и собственным секретом. admin-путь остальных сервисов не трогаем.
# Блок идемпотентен (CREATE/ALTER ROLE), выполняется ПОСЛЕ миграции, чтобы
# таблицы уже существовали для GRANT SELECT ON ALL TABLES.
echo "Creating read-only acm_ro role (SEC-24)..."
if [ ! -s /run/secrets/acm_db_password ]; then
    echo "acm_db_password secret is missing or empty" >&2
    exit 1
fi
ACM_RO_PASSWORD="$(cat /run/secrets/acm_db_password)"
# Пароль НЕ интерполируется в SQL через shell-конкатенацию. Роль создаётся БЕЗ
# пароля внутри DO-блока (psql НЕ подставляет переменные внутри dollar-quoted
# тела), а пароль выставляется отдельным top-level `ALTER ROLE ... PASSWORD
# :'acm_pw'`, где psql интерполирует :'acm_pw' как безопасно-квотированный литерал.
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    -v acm_pw="$ACM_RO_PASSWORD" <<'SQL'
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'acm_ro') THEN
    CREATE ROLE acm_ro LOGIN;
  END IF;
END
$$;
ALTER ROLE acm_ro PASSWORD :'acm_pw';
GRANT CONNECT ON DATABASE scud_users TO acm_ro;
GRANT USAGE ON SCHEMA public TO acm_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO acm_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO acm_ro;
SQL

echo "Read-only acm_ro role ready."
