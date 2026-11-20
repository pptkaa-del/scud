#!/usr/bin/env bash
# Smoke test for dbprobe.sh against a LIVE dev stack.
#
# Brings up pgbouncer_user + postgres_user, builds the certified agent2 image
# (--target production, carries psql), and runs the probe for one pgb metric
# (cl_waiting) and one pg metric (numbackends). Each MUST be a single integer
# on stdout. Creds come from the mounted Docker secrets (pgb_admin_password +
# postgres_user_password); passwords are never echoed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
IMAGE=scud-zabbix-agent2:dbprobe-test

# On Git Bash for Windows, Docker needs Windows-style paths for bind mounts
# and build contexts. cygpath -w converts /c/... -> C:\...
if command -v cygpath >/dev/null 2>&1; then
    WIN_REPO="$(cygpath -w "$REPO")"
    WIN_HERE="$(cygpath -w "$HERE")"
else
    WIN_REPO="$REPO"
    WIN_HERE="$HERE"
fi

cleanup() { (cd "$REPO/backend" && docker compose down -v) >/dev/null 2>&1 || true; }
trap cleanup EXIT

# 1. Secrets + minimal stack (postgres_user pulled in via pgbouncer depends_on).
bash "$REPO/backend/scripts/seed-dev-secrets.sh"
(cd "$REPO/backend" && docker compose up -d --wait pgbouncer_user postgres_user)

# 2. Certified production image (psql baked in by Task D2).
docker build -t "$IMAGE" --target production "$WIN_HERE"

# Resolve the project's backend_net (compose prefixes it with the project name).
NET="$(docker network ls --format '{{.Name}}' | grep -E '_backend_net$' | head -n1)"
[ -n "$NET" ] || { echo "FAIL: backend_net not found" >&2; exit 1; }

# run_probe <source> <host> <metric> <secret_file>
# MSYS_NO_PATHCONV + MSYS2_ARG_CONV_EXCL prevent Git Bash from rewriting
# container-side absolute paths (/usr/local/bin/..., /run/secrets/...) into
# Windows host paths.
run_probe() {
    MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
    docker run --rm --network "$NET" \
        --entrypoint sh \
        -v "${WIN_HERE}/dbprobe.sh:/usr/local/bin/dbprobe.sh:ro" \
        -v "${WIN_REPO}/backend/secrets/$4:/run/secrets/$4:ro" \
        "$IMAGE" /usr/local/bin/dbprobe.sh "$1" "$2" "$3"
}

is_int() { printf '%s' "$1" | grep -qE '^[0-9]+$'; }

CL_WAITING="$(run_probe pgb pgbouncer_user cl_waiting pgb_admin_password)"
is_int "$CL_WAITING" || { echo "FAIL: pgb cl_waiting not an integer: [$CL_WAITING]" >&2; exit 1; }

NUMBACKENDS="$(run_probe pg postgres_user numbackends postgres_user_password)"
is_int "$NUMBACKENDS" || { echo "FAIL: pg numbackends not an integer: [$NUMBACKENDS]" >&2; exit 1; }

echo "OK dbprobe smoke: cl_waiting=$CL_WAITING numbackends=$NUMBACKENDS"
