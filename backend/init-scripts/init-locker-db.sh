#!/bin/bash
set -e

echo "Starting locker_api database migrations..."

# Locker API migrations
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /migrations/001_init.sql

echo "Locker API migrations completed successfully."
