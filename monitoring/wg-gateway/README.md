# wg_gateway — WireGuard sidecar for the Yeastar SMS gateway

The Yeastar NeoGate TG200 GSM gateway (SMS OTP delivery, `notification_api`'s
SMS transport) is reachable only over WireGuard, at `10.8.0.19:7777` inside
the tunnel's `10.8.0.0/24` range. This sidecar brings the tunnel up and
forwards a single TCP port, so the rest of the stack can talk to the gateway
as an ordinary host on `backend_net` — no service other than this container
gets `NET_ADMIN` or touches `/dev/net/tun`.

## What it does

1. `entrypoint.sh` renders `/etc/wireguard/wg0.conf` from env vars + two
   docker secrets (private key, preshared key) and runs `wg-quick up wg0`.
2. It then `exec`s `socat`, listening on `:${FORWARD_PORT}` and forwarding
   every connection to `${SMS_GATEWAY_HOST}:${SMS_GATEWAY_PORT}` through the
   tunnel.
3. `notification_api` is configured with `SMS_GATEWAY_URL=http://wg_gateway:7777`
   and never sees the tunnel or the real gateway address.

## Files

| File | Purpose |
|---|---|
| `Dockerfile` | `ubuntu:24.04` image with `wireguard-tools`, `socat`, `iproute2`, `iptables`, `ca-certificates` |
| `entrypoint.sh` | Renders `wg0.conf`, brings the interface up, execs the `socat` forward |
| `test_build.sh` | Builds the image and runs a conf-generation smoke test (fake `wg-quick`/`socat`, no real tunnel) |

## Certification note

This is a network infrastructure sidecar, **outside the Astra certification
perimeter** — the same category as the host TLS terminator described in
`backend/DEPLOYMENT.md`. `wireguard-tools` is not available in certified
Astra `main`, and WireGuard has no certified equivalent, so the base image
is plain `ubuntu:24.04` rather than the Astra `ubi18-python311` image used
for backend services. This container does not process any SCUD application
data — it only relays opaque TCP bytes between `notification_api` and the
gateway's HTTP-CGI SMS API.

## Environment variables

| Variable | Required | Default | Description |
|---|---|---|---|
| `WG_ADDRESS` | yes | — | This peer's tunnel address, CIDR form (e.g. `10.8.0.14/24`) — `Address =` in `wg0.conf` |
| `WG_PEER_PUBKEY` | yes | — | Remote WireGuard peer's public key |
| `WG_ENDPOINT` | yes | — | Remote peer's `host:port` (e.g. `94.103.85.33:29156`) |
| `WG_ALLOWED_IPS` | no | `10.8.0.0/24` | Split-tunnel route — only this range goes through the tunnel |
| `WG_KEEPALIVE` | no | `25` | `PersistentKeepalive` seconds, keeps NAT/firewall state alive |
| `SMS_GATEWAY_HOST` | yes | — | Gateway address inside the tunnel (e.g. `10.8.0.19`) |
| `SMS_GATEWAY_PORT` | no | `7777` | Gateway's HTTP-CGI port, reached through the tunnel |
| `FORWARD_PORT` | no | `7777` | Local port `socat` listens on for `backend_net` clients |

## Secrets

| Secret (`/run/secrets/<name>`) | Required | Description |
|---|---|---|
| `wg_private_key` | yes | This peer's WireGuard private key |
| `wg_preshared_key` | no | Optional PSK; when the file is absent or empty, `PresharedKey` is omitted from `wg0.conf`. `deploy.sh` still enables the `sms` profile only when it is filled |

Neither secret is ever logged; `entrypoint.sh` writes `wg0.conf` under
`umask 077` before populating it.

## Split-tunnel / no-DNS / keepalive rationale

- **Split-tunnel (`AllowedIPs=10.8.0.0/24`), not a full tunnel.** Only traffic
  to the gateway's `/24` is routed over WireGuard; everything else (DNS
  lookups, inter-service calls on `backend_net`, package installs at build
  time) is unaffected. This keeps the sidecar's blast radius to exactly one
  destination network.
- **No `DNS =` line in `wg0.conf`.** The tunnel does not override the
  container's (or host's) resolver. The gateway is addressed by IP
  (`SMS_GATEWAY_HOST`), so there is nothing to resolve through the tunnel,
  and other containers' name resolution stays untouched.
- **`PersistentKeepalive = 25`.** The gateway side is typically behind NAT;
  without a keepalive the handshake state on intermediate NAT/firewalls
  expires and inbound responses stop arriving. 25 s is the conventional
  WireGuard default for peers behind NAT and matches what was verified
  working during live reachability testing against the stand's gateway.

## Running it on the stand

The sidecar is wired into `backend/docker-compose.yaml` as the `wg_gateway`
service under the `sms` profile (see the compose task in this feature's
plan) — it does not start on a plain `docker compose up`. On the stand:

```bash
cd backend
# Real values, not the seed-dev-secrets.sh empty defaults:
printf '%s' "<private key>"   > secrets/wg_private_key
printf '%s' "<preshared key>" > secrets/wg_preshared_key
chmod 644 secrets/wg_private_key secrets/wg_preshared_key   # bind-mount readability

# .env: WG_PEER_PUBKEY, WG_ENDPOINT, SMS_GATEWAY_URL=http://wg_gateway:7777
docker compose --profile sms up -d --build wg_gateway
docker compose --profile sms logs -f wg_gateway   # expect "wg0 up; forwarding :7777 -> ..."
docker compose exec wg_gateway wg show wg0 latest-handshakes   # expect a recent timestamp
```

The container requests `cap_add: [NET_ADMIN]` and `/dev/net/tun` — this is
the only place in the stack those privileges are granted. Everything else on
`backend_net`, including `notification_api`, talks to `wg_gateway:7777` as a
plain TCP host and needs no elevated privileges of its own.

## Local build/test

```bash
sh monitoring/wg-gateway/test_build.sh
```

Builds `scud-wg-gateway:test` and runs the entrypoint with a fake
`wg-quick`/`socat` on `PATH`, so only the config templating is exercised —
no real tunnel is created. Expected output ends with `CONF-OK`.

```bash
docker run --rm -v "$PWD/monitoring/wg-gateway:/src" koalaman/shellcheck:stable /src/entrypoint.sh /src/test_build.sh
docker run --rm -i hadolint/hadolint < monitoring/wg-gateway/Dockerfile
```

Both must be clean.
