#!/bin/bash
set -e

echo "Starting auth_api database migrations..."

# Auth API migrations
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f /migrations/001_init.sql

echo "Auth API migrations completed successfully."
