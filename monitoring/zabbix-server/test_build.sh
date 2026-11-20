#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -t scud-zabbix-server:test --target production "$HERE"
# production stage must NOT contain the extended repository
docker run --rm scud-zabbix-server:test sh -c 'grep -q extended /etc/apt/sources.list && exit 1 || true'
# server binary and psql client must be installed from main
docker run --rm scud-zabbix-server:test sh -c 'test -x /usr/sbin/zabbix_server && command -v psql >/dev/null'
echo "OK zabbix-server image"
