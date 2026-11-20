#!/usr/bin/env bash
# Эмуляция подключённых релейных плат на сервере с установленной СКУД.
#
#   sudo bash /opt/scud/emulator/run-device.sh            # 1 плата, relay_id 5001
#   sudo bash /opt/scud/emulator/run-device.sh --events   # + циклические события безопасности
#   sudo bash /opt/scud/emulator/run-device.sh stop       # остановить
#   sudo bash /opt/scud/emulator/run-device.sh logs       # журнал
#
# Что делает: собирает образ эмулятора, создаёт в СКУД шкаф RELAY-<id> (transport=mqtt)
# от имени администратора (вход паролем — у администратора не должна быть включена 2FA),
# запускает контейнер scud-relay-<id>, который подключается к брокеру localhost:1883.
# Переменные: SCUD_DIR (/opt/scud), RELAY_ID (5001), RELAY_COUNT (1),
# SCUD_GATEWAY_URL (http://localhost), MQTT_HOST (127.0.0.1), MQTT_PORT (1883).
# Остальные аргументы передаются в `scud relay run` (--events, --photos, --version ...).
set -euo pipefail

DIR=${SCUD_DIR:-/opt/scud}
ID=${RELAY_ID:-5001}
COUNT=${RELAY_COUNT:-1}
GW=${SCUD_GATEWAY_URL:-http://localhost}
MQTT_HOST=${MQTT_HOST:-127.0.0.1}
MQTT_PORT=${MQTT_PORT:-1883}
IMAGE=scud-relay:local
NAME=scud-relay-$ID

info() { printf '==> %s\n' "$*"; }
die() {
    printf 'ОШИБКА: %s\n' "$*" >&2
    exit 1
}

case "${1:-}" in
    stop)
        docker rm -f "$NAME" >/dev/null 2>&1 && info "остановлен $NAME" || info "$NAME не запущен"
        exit 0
        ;;
    logs)
        exec docker logs -f --tail 100 "$NAME"
        ;;
esac

[ "$(id -u)" -eq 0 ] || die "запустите от root: sudo bash $0"
[ -f "$DIR/backend/.env" ] || die "в $DIR нет установки СКУД"
[[ $ID =~ ^[0-9]+$ ]] && [[ $COUNT =~ ^[0-9]+$ ]] || die "RELAY_ID и RELAY_COUNT — числа"

PASS=$(tr -d '\r\n' <"$DIR/backend/secrets/admin_password") || die "нет secrets/admin_password"
PHONE=$(grep -E '^ADMIN_PHONE=' "$DIR/backend/.env" | tail -n1 | cut -d= -f2- | tr -d "'\"")
PHONE=${PHONE:-+79000000000}

info "Сборка образа эмулятора ($IMAGE)"
docker build -q -t "$IMAGE" "$DIR/emulator" >/dev/null

info "Создание шкафов RELAY-$ID..RELAY-$((ID + COUNT - 1)) в СКУД ($GW, вход $PHONE)"
set +e
out=$(docker run --rm --network host -e SCUD_ADMIN_PASSWORD="$PASS" --entrypoint scud "$IMAGE" \
    relay provision --count "$COUNT" --base-id "$ID" --gateway-url "$GW" --phone "$PHONE" 2>&1)
rc=$?
set -e
printf '%s\n' "$out"
# rc=1 и «skipped (status=409)» — шкафы уже есть, это нормально для повторного запуска
if [ "$rc" -ne 0 ] && ! grep -q 'status=409' <<<"$out"; then
    die "не удалось создать шкаф (у администратора включена 2FA или СКУД недоступна по $GW)"
fi

info "Запуск платы $NAME -> $MQTT_HOST:$MQTT_PORT"
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart unless-stopped --network host "$IMAGE" \
    --count "$COUNT" --base-id "$ID" --host "$MQTT_HOST" --port "$MQTT_PORT" "$@" >/dev/null

sleep 5
docker logs --tail 20 "$NAME" 2>&1 || true
info "Готово. Шкаф RELAY-$ID должен стать «онлайн» в веб-интерфейсе в течение минуты."
note_cmd="sudo bash $DIR/emulator/run-device.sh"
printf '    журнал:     %s logs\n    остановить: %s stop\n' "$note_cmd" "$note_cmd"
