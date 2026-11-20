#!/bin/sh
# Приводит aclfile тома к дефолтам образа, сохраняя per-device блоки.
#
# Том mqtt_config сеется из образа только при первом запуске, поэтому без этой
# сверки правка aclfile в образе никогда не доезжает до уже развёрнутого стенда
# и брокер молча работает по устаревшим правам. Пароль backend-пользователя
# сверяется на каждом старте по той же причине (INF-06).
#
# Базовая часть (backend-пользователь + pattern-строки) берётся из дефолтов,
# блоки `user relay-*`, которые дописывает провижининг, переносятся как есть.
set -eu

CONF_DIR="${1:-/mosquitto/config}"
DEFAULTS_DIR="${2:-/opt/mqtt-defaults}"

LIVE="$CONF_DIR/aclfile"
DEFAULTS="$DEFAULTS_DIR/aclfile"

[ -f "$DEFAULTS" ] || exit 0

if [ ! -f "$LIVE" ]; then
    cp "$DEFAULTS" "$LIVE"
    chmod 0666 "$LIVE"
    exit 0
fi

TMP="$LIVE.tmp.$$"
cat "$DEFAULTS" > "$TMP"
# Провижининг дописывает блоки в конец, поэтому всё от первого `user relay-`
# и до конца файла — это устройства.
awk '/^user relay-/ { found = 1 } found { print }' "$LIVE" >> "$TMP"
mv "$TMP" "$LIVE"
chmod 0666 "$LIVE"
