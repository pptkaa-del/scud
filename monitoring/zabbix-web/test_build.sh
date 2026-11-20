#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
docker build -t scud-zabbix-web:test -f "$HERE/Dockerfile" "$HERE"
docker run --rm scud-zabbix-web:test sh -c 'grep -q extended /etc/apt/sources.list && exit 1 || true'
# frontend and php-fpm are present, mod_php is NOT installed
docker run --rm scud-zabbix-web:test sh -c 'test -d /usr/share/zabbix && command -v php-fpm8.2 >/dev/null'
docker run --rm scud-zabbix-web:test sh -c 'dpkg -l | grep -q libapache2-mod-php && exit 1 || true'
# en_US/ru_RU UTF-8 locales are generated (Zabbix frontend setlocale, else
# "Locale for language en_US is not found" and the UI cannot translate)
docker run --rm scud-zabbix-web:test sh -c 'locale -a | grep -qi "en_US.utf8" && locale -a | grep -qi "ru_RU.utf8"'
echo "OK zabbix-web image"
