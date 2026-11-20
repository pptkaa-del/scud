#!/bin/sh
set -eu

: "${WG_ADDRESS:?}"; : "${WG_PEER_PUBKEY:?}"; : "${WG_ENDPOINT:?}"
: "${WG_ALLOWED_IPS:=10.8.0.0/24}"; : "${WG_KEEPALIVE:=25}"
: "${SMS_GATEWAY_HOST:?}"; : "${SMS_GATEWAY_PORT:=7777}"; : "${FORWARD_PORT:=7777}"

PRIV="$(cat /run/secrets/wg_private_key)"
: "${PRIV:?wg_private_key secret is empty}"
PSK_LINE=""
if [ -s /run/secrets/wg_preshared_key ]; then
  PSK_LINE="PresharedKey = $(cat /run/secrets/wg_preshared_key)"
fi

umask 077
mkdir -p /etc/wireguard
cat > /etc/wireguard/wg0.conf <<EOF
[Interface]
PrivateKey = ${PRIV}
Address = ${WG_ADDRESS}

[Peer]
PublicKey = ${WG_PEER_PUBKEY}
${PSK_LINE}
AllowedIPs = ${WG_ALLOWED_IPS}
PersistentKeepalive = ${WG_KEEPALIVE}
Endpoint = ${WG_ENDPOINT}
EOF

wg-quick up wg0
echo "wg0 up; forwarding :${FORWARD_PORT} -> ${SMS_GATEWAY_HOST}:${SMS_GATEWAY_PORT}"
exec socat -d TCP-LISTEN:"${FORWARD_PORT}",fork,reuseaddr TCP:"${SMS_GATEWAY_HOST}":"${SMS_GATEWAY_PORT}"
