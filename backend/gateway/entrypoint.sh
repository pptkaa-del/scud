#!/bin/sh
# Apache gateway entrypoint.
#
# Подставляет env-переменные в apache.conf перед запуском (Apache
# в `Header set` не разворачивает env напрямую — нужен либо Define,
# либо sed-replace на старте; выбран sed для простоты).
set -eu

CONF=/etc/apache2/sites-available/gateway.conf

# CORS: дефолт `*` для dev. В prod установите конкретный origin фронтенда
# (Access-Control-Allow-Credentials с `*` несовместим, а cookie у нас Secure+SameSite=Strict).
CORS_ALLOWED_ORIGIN="${CORS_ALLOWED_ORIGIN:-*}"

# sed экранирует только &, |, \ в значении — для большинства origin-URI
# этого достаточно; если нужно что-то экзотичное, расширьте список.
ESCAPED=$(printf '%s' "${CORS_ALLOWED_ORIGIN}" | sed 's/[&|\\]/\\&/g')
sed -i "s|\${CORS_ALLOWED_ORIGIN}|${ESCAPED}|g" "${CONF}"

# TRUSTED_PROXY_CIDR (SEC-07): доверенный хоп для mod_remoteip — адрес/CIDR
# TLS-терминатора, чьему X-Real-IP можно верить. Дефолт — loopback под
# документированную nginx-схему (nginx → 127.0.0.1:80). В production
# задайте реальный адрес терминатора через env. Несколько значений можно
# перечислить через пробел.
TRUSTED_PROXY_CIDR="${TRUSTED_PROXY_CIDR:-127.0.0.1/8}"
ESCAPED_CIDR=$(printf '%s' "${TRUSTED_PROXY_CIDR}" | sed 's/[&|\\]/\\&/g')
sed -i "s|\${TRUSTED_PROXY_CIDR}|${ESCAPED_CIDR}|g" "${CONF}"

exec apache2ctl -D FOREGROUND
