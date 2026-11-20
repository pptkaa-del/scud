#!/bin/sh
set -eu
cd "$(dirname "$0")"
docker build -t scud-wg-gateway:test .
# Conf-generation smoke: run entrypoint with a fake wg-quick/socat on PATH so
# it only exercises the config templating, not a real tunnel.
docker run --rm --entrypoint sh scud-wg-gateway:test -c '
  mkdir -p /run/secrets && echo PRIVKEY > /run/secrets/wg_private_key && echo PSK > /run/secrets/wg_preshared_key
  printf "#!/bin/sh\nexit 0\n" > /usr/local/bin/wg-quick && chmod +x /usr/local/bin/wg-quick
  printf "#!/bin/sh\necho socat-ok\n" > /usr/local/bin/socat && chmod +x /usr/local/bin/socat
  WG_ADDRESS=10.8.0.14/24 WG_PEER_PUBKEY=PK WG_ENDPOINT=1.2.3.4:29156 SMS_GATEWAY_HOST=10.8.0.19 /entrypoint.sh
  grep -q "Address = 10.8.0.14/24" /etc/wireguard/wg0.conf && echo CONF-OK
'
