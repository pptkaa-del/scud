#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# On Git Bash for Windows, convert POSIX path to Windows path for Docker
if command -v cygpath >/dev/null 2>&1; then
    BUILD_CTX="$(cygpath -w "$HERE")"
else
    BUILD_CTX="$HERE"
fi
docker build -t scud-syslog-ng:test --target production "$BUILD_CTX"
# production stage must NOT contain the extended repository
docker run --rm scud-syslog-ng:test sh -c 'grep -q extended /etc/apt/sources.list && exit 1 || true'
# syslog-ng binary and ctl must be installed from main
docker run --rm scud-syslog-ng:test sh -c 'test -x /usr/sbin/syslog-ng && test -x /usr/sbin/syslog-ng-ctl'
# config must be baked in and parse cleanly (--syntax-only exits 0 on success)
docker run --rm --entrypoint sh scud-syslog-ng:test -c 'syslog-ng --syntax-only -f /etc/syslog-ng/syslog-ng.conf'
# logrotate must be installed (Astra main) and the rotation stanza baked in + valid
docker run --rm --entrypoint sh scud-syslog-ng:test -c 'command -v logrotate >/dev/null && test -f /etc/logrotate.d/scud-all'
docker run --rm --entrypoint sh scud-syslog-ng:test -c 'logrotate -d /etc/logrotate.d/scud-all >/dev/null 2>&1'
# the service allowlist filter must be present in the baked config
docker run --rm --entrypoint sh scud-syslog-ng:test -c 'grep -q f_scud_services /etc/syslog-ng/syslog-ng.conf'
# the rotation entrypoint must be executable
docker run --rm --entrypoint sh scud-syslog-ng:test -c 'test -x /usr/local/bin/entrypoint.sh'
echo "OK syslog-ng image"
