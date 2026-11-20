#!/bin/bash
set -e

echo "Running History API database migrations..."

# Применяем миграцию
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    \echo 'Applying migration 001_init_history.sql...'
EOSQL

# Применяем файлы миграций
for migration in /migrations/*.sql; do
    if [ -f "$migration" ]; then
        echo "Applying migration: $migration"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$migration"
    fi
done

echo "History API database initialization completed!"
