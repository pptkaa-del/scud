#!/bin/sh
set -eu

CONF_DIR=/mosquitto/config
CONF="$CONF_DIR/mosquitto.conf"
PW="$CONF_DIR/passwordfile"
ACL="$CONF_DIR/aclfile"

# --- Broker (backend service) credential ---------------------------------
# The scud_backend password is NOT committed in plaintext. It is sourced at
# startup from (in priority order):
#   1. Docker secret  /run/secrets/mqtt_password
#   2. env            MQTT_PASSWORD
#   3. dev fallback   scud_backend_dev_password  (DEV ONLY; prod always sets 1 or 2)
# This mirrors shared.utils.read_secret precedence so the broker and the
# backend (locker_api) always agree on the same value.
MQTT_BACKEND_USER="${MQTT_USER:-scud_backend}"
read_backend_password() {
    if [ -s /run/secrets/mqtt_password ]; then
        # strip surrounding whitespace/newline (echo ">file" adds \n)
        tr -d '\r\n' < /run/secrets/mqtt_password
        return
    fi
    printf '%s' "${MQTT_PASSWORD:-scud_backend_dev_password}"
}

# Seed the (possibly empty mounted) config volume from baked image defaults.
# On first run the named volume is empty; copy defaults so mosquitto has a
# config/acl to start from. mosquitto.conf/aclfile are static templates and are
# only copied when absent (provisioning appends per-device ACL entries, so we
# must not clobber them). passwordfile is RECONCILED below on every start.
# mosquitto.conf копируется один раз (его не правит никто, кроме образа);
# aclfile сверяется с дефолтами на каждом старте — иначе правка прав в
# образе не доезжает до уже существующего тома.
[ -f "$CONF_DIR/mosquitto.conf" ] || cp /opt/mqtt-defaults/mosquitto.conf "$CONF_DIR/mosquitto.conf"
/acl-reconcile.sh "$CONF_DIR" /opt/mqtt-defaults

reconcile_backend_credential() {
    # Rewrite the scud_backend line from the secret/env on every start (INF-06:
    # the old entrypoint seeded passwordfile once and never reconciled, so a
    # rotated secret never reached an existing config volume). Per-device
    # relay-{id} lines appended by provisioning are PRESERVED — we only touch
    # the single scud_backend row.
    pw="$(read_backend_password)"
    tmp="$PW.tmp.$$"
    # Keep every existing line that is NOT the backend user, then re-add it.
    if [ -f "$PW" ]; then
        grep -v "^${MQTT_BACKEND_USER}:" "$PW" > "$tmp" 2>/dev/null || : > "$tmp"
    else
        : > "$tmp"
    fi
    printf '%s:%s\n' "$MQTT_BACKEND_USER" "$pw" >> "$tmp"
    mv "$tmp" "$PW"
    chmod 0666 "$PW"
}

hash_passwords() {
    # Hash any plaintext entries in place. mosquitto_passwd -U is a no-op on
    # already-hashed lines. Do NOT swallow errors: a hashing failure must surface
    # (otherwise the broker would run with an unusable password file and reject all).
    #
    # mosquitto_passwd is only built/shipped when the broker is compiled WITH_TLS=yes
    # (it links OpenSSL). This image is WITH_TLS=no for certification (zero extra
    # libs), so the binary is absent and password_file entries are compared as
    # plaintext by the broker (src/security_default.c). When the tool is missing,
    # leave the file as-is — plaintext is exactly what the no-TLS broker expects.
    if [ -f "$PW" ] && command -v mosquitto_passwd >/dev/null 2>&1; then
        mosquitto_passwd -U "$PW"
    fi
}

reconcile_backend_credential
hash_passwords

# Start the broker (it drops to the 'mosquitto' user per mosquitto.conf).
mosquitto -c "$CONF" &
BROKER_PID=$!

# Reload watcher: on any change to password/acl files, re-hash + SIGHUP.
# Pure-shell poll (inotify-tools is not in Astra certified main).
#
# Change detection is a CONTENT signature (cksum = size + CRC32) of both files,
# NOT mtime. mtime at 1-second granularity (ls --time-style=+%s) misses a
# provision append that lands in the SAME second as the prior sample — the
# watcher would never SIGHUP and the broker would reject the new device until
# the next unrelated write. cksum changes on any byte change regardless of when
# it happened.
sig() { kill -TERM "$BROKER_PID" 2>/dev/null; exit 0; }
trap sig TERM INT
LAST=""
while kill -0 "$BROKER_PID" 2>/dev/null; do
    NOW=$(cksum "$PW" "$ACL" 2>/dev/null | tr '\n' ' ')
    if [ "$NOW" != "$LAST" ] && [ -n "$LAST" ]; then
        hash_passwords
        kill -HUP "$BROKER_PID" 2>/dev/null || true
    fi
    LAST="$NOW"
    sleep 2
done
wait "$BROKER_PID"
