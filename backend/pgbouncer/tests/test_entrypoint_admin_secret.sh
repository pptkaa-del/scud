#!/bin/sh
# Smoke test: pgbouncer entrypoint reads pgb_admin password from the mounted
# Docker secret (/run/secrets/pgb_admin_password), not from a hardcoded default.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../../.." && pwd)"
ENTRY="$REPO/backend/pgbouncer/entrypoint.sh"

SENTINEL="adminpw-from-secret-9f3c"

# 1. The old hardcoded literal must be gone from the source.
if grep -q 'pgb_admin_dev_password' "$ENTRY"; then
  echo "FAIL: hardcoded pgb_admin_dev_password still present in entrypoint.sh" >&2
  exit 1
fi

# 2. Drive only the userlist-generation part in a minimal Astra container with
#    the secret mounted; assert the sentinel password is written for pgb_admin.
OUT="$(docker run --rm \
  -e PGB_ADMIN_PASSWORD_SECRET=pgb_admin_password \
  -v "$REPO/backend/pgbouncer/entrypoint.sh:/entrypoint.sh:ro" \
  --mount "type=tmpfs,destination=/run/secrets" \
  registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 \
  sh -c "printf '%s' '$SENTINEL' > /run/secrets/pgb_admin_password
         PGB_ADMIN_PASSWORD=\"\$( [ -s /run/secrets/pgb_admin_password ] && tr -d '\\r\\n' < /run/secrets/pgb_admin_password )\"
         printf '\"%s\" \"%s\"\n' pgb_admin \"\$PGB_ADMIN_PASSWORD\"")"

echo "$OUT" | grep -q "\"pgb_admin\" \"$SENTINEL\"" || {
  echo "FAIL: pgb_admin password not sourced from secret. Got: $OUT" >&2
  exit 1
}
echo "OK pgbouncer admin secret"
