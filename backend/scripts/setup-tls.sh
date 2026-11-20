#!/usr/bin/env bash
# HTTPS для установленной СКУД: nginx + сертификат Let's Encrypt перед apache_gateway.
#
#   sudo bash /opt/scud/backend/scripts/setup-tls.sh scud.example.com admin@example.com
#
# Перед запуском A-запись домена должна указывать на этот сервер, порты 80 и 443 открыты.
# Что делает:
#   1. переносит apache_gateway с 0.0.0.0:80 на 127.0.0.1:8080 (docker-compose.override.yaml);
#   2. ставит nginx и certbot, проксирует домен на 127.0.0.1:8080;
#   3. получает сертификат (автопродление — таймер certbot), включает редирект HTTP -> HTTPS;
#   4. deploy.sh update --reconfigure с адресом https://домен (CORS, Secure-cookie).
# Локальные проверки deploy.sh (http://localhost/health) идут через nginx без редиректа.
set -euo pipefail

DOMAIN=${1:-}
EMAIL=${2:-}
DIR=${SCUD_DIR:-/opt/scud}
GW_PORT=${SCUD_GATEWAY_PORT:-8080}
BACKEND=$DIR/backend
OVERRIDE=$BACKEND/docker-compose.override.yaml
SITE=/etc/nginx/sites-available/scud.conf

info() { printf '==> %s\n' "$*"; }
die() {
    printf 'ОШИБКА: %s\n' "$*" >&2
    exit 1
}

[ -n "$DOMAIN" ] && [ -n "$EMAIL" ] || die "использование: setup-tls.sh ДОМЕН ПОЧТА"
[[ $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}$ ]] || die "недопустимый домен: $DOMAIN"
[[ $EMAIL =~ ^[^[:space:]@]+@[^[:space:]@]+$ ]] || die "недопустимая почта: $EMAIL"
[ "$(id -u)" -eq 0 ] || die "запустите от root"
[ -f "$BACKEND/deploy.conf" ] || die "в $DIR нет установки СКУД (сначала install.sh)"
command -v apt-get >/dev/null 2>&1 || die "нужен apt (Debian/Ubuntu/Astra)"

# --- 1. gateway только на loopback ---
if ! grep -q 'apache_gateway:' "$OVERRIDE" 2>/dev/null; then
    info "apache_gateway -> 127.0.0.1:$GW_PORT ($OVERRIDE)"
    [ -s "$OVERRIDE" ] || printf 'services:\n' >"$OVERRIDE"
    grep -q '^services:' "$OVERRIDE" || die "$OVERRIDE без секции services: — поправьте вручную"
    printf '  apache_gateway:\n    ports: !override\n      - "127.0.0.1:%s:80"\n' "$GW_PORT" >>"$OVERRIDE"
else
    info "apache_gateway уже есть в $OVERRIDE — не трогаю (ожидается порт 127.0.0.1:$GW_PORT)"
fi
(cd "$BACKEND" && docker compose -p backend config --quiet) || die "docker compose не принял $OVERRIDE (нужен compose 2.24+ для !override)"
(cd "$BACKEND" && docker compose -p backend up -d --no-build --no-deps apache_gateway)

# --- 2. nginx ---
info "Установка nginx и certbot"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx certbot python3-certbot-nginx

info "Конфиг nginx: $SITE"
cat >"$SITE" <<EOF
# СКУД: TLS-терминатор перед apache_gateway (backend/scripts/setup-tls.sh)
map \$http_upgrade \$scud_connection { default upgrade; '' close; }

# Локальные проверки deploy.sh: http://localhost/ без редиректа на HTTPS.
server {
    listen 127.0.0.1:80;
    listen [::1]:80;
    server_name localhost;
    location / {
        proxy_pass http://127.0.0.1:$GW_PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}

server {
    listen 80;
    listen [::]:80;
    server_name $DOMAIN;
    client_max_body_size 20m;

    location / {
        proxy_pass http://127.0.0.1:$GW_PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection \$scud_connection;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;
    }
}
EOF
ln -sf "$SITE" /etc/nginx/sites-enabled/scud.conf
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl enable --now nginx
systemctl reload nginx

# --- 3. сертификат ---
info "Сертификат Let's Encrypt для $DOMAIN"
certbot --nginx -d "$DOMAIN" -m "$EMAIL" --agree-tos --no-eff-email --redirect --non-interactive
systemctl reload nginx

# --- 4. адрес сайта https:// в настройках СКУД ---
info "deploy.sh update --reconfigure (адрес https://$DOMAIN)"
SCUD_PUBLIC_URL="https://$DOMAIN" SCUD_TRUSTED_PROXY_CIDR=127.0.0.1/8 \
    bash "$BACKEND/scripts/deploy.sh" update --dir "$DIR" --reconfigure -y

info "Готово: https://$DOMAIN"
