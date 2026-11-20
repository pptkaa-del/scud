#!/bin/sh
set -e

mkdir -p /run/php

# Zabbix UI reads /etc/zabbix/web/zabbix.conf.php; DB params from env.
CONF=/etc/zabbix/zabbix.conf.php
mkdir -p /etc/zabbix

if [ -n "${DB_PASSWORD_FILE:-}" ] && [ -s "${DB_PASSWORD_FILE}" ]; then
    DB_PASSWORD="$(cat "${DB_PASSWORD_FILE}")"
fi

cat > "$CONF" <<PHP
<?php
\$DB['TYPE']     = 'POSTGRESQL';
\$DB['SERVER']   = '${DB_SERVER_HOST:-postgres_zabbix}';
\$DB['PORT']     = '5432';
\$DB['DATABASE'] = '${DB_NAME:-zabbix}';
\$DB['USER']     = '${DB_USER:-zabbix}';
\$DB['PASSWORD'] = '${DB_PASSWORD:-}';
\$ZBX_SERVER      = '${ZBX_SERVER_HOST:-zabbix_server}';
\$ZBX_SERVER_PORT = '${ZBX_SERVER_PORT:-10051}';
\$ZBX_SERVER_NAME = 'ScudDocs';
\$IMAGE_FORMAT_DEFAULT = IMAGE_FORMAT_PNG;
PHP

php-fpm8.2 -D
exec apache2ctl -DFOREGROUND
