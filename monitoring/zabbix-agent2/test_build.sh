#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# On Git Bash for Windows, convert POSIX path to Windows path for Docker
if command -v cygpath >/dev/null 2>&1; then
    BUILD_CTX="$(cygpath -w "$HERE")"
else
    BUILD_CTX="$HERE"
fi
docker build -t scud-zabbix-agent2:test --target production "$BUILD_CTX"
# production stage must NOT contain the extended repository
docker run --rm --entrypoint sh scud-zabbix-agent2:test -c 'grep -q extended /etc/apt/sources.list && exit 1 || true'
# agent2 binary and psql client must be installed from main
docker run --rm --entrypoint sh scud-zabbix-agent2:test -c 'test -x /usr/sbin/zabbix_agent2 && command -v psql >/dev/null'
# UserParameter conf must be baked in and define scud.dbprobe.
docker run --rm --entrypoint sh scud-zabbix-agent2:test \
  -c 'grep -q "scud.dbprobe" /etc/zabbix/zabbix_agent2.d/scud-dbprobe.conf'
# Timeout drop-in must be baked in (passive dbprobe checks need >3s; max 30 in 7.0).
docker run --rm --entrypoint sh scud-zabbix-agent2:test \
  -c 'grep -q "^Timeout=30" /etc/zabbix/zabbix_agent2.d/scud-timeout.conf'
# dbprobe script must be present and executable at the contracted path.
docker run --rm --entrypoint sh scud-zabbix-agent2:test \
  -c 'test -x /usr/local/bin/dbprobe.sh'
# agent ping must work (binary self-test); output is "[s|1]" in active format.
docker run --rm --entrypoint sh scud-zabbix-agent2:test \
  -c 'mkdir -p /run/zabbix && zabbix_agent2 -t agent.ping 2>/dev/null | grep -q "|1]"'
echo "OK zabbix-agent2 image"
