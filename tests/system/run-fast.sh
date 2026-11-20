#!/usr/bin/env bash
# Run E2E tests with fast-mode acceleration (env-file pattern).
#
# The relay device double (scud_relay) is mounted directly into the system_tests
# container and driven in-process by RelayRunner, so no separate emulator service
# needs to be started — only the backend stack (broker + services).
set -e
cd "$(dirname "$0")/../.."
( cd backend && docker compose --env-file .env.fast up -d )
( cd tests/system && docker compose --env-file .env.fast run --rm system_tests "$@" )
