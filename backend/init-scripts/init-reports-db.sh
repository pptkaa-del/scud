#!/bin/bash
set -e

echo "Starting reports_api database migrations..."

# Reports API migrations
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /migrations/001_init.sql

echo "Reports API migrations completed successfully."
