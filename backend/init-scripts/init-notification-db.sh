#!/bin/bash
set -e

echo "Starting notification_api database migrations..."

# Notification API migrations
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /migrations/001_init.sql

echo "Notification API migrations completed successfully."
