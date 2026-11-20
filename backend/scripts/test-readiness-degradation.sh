#!/usr/bin/env bash
# test-readiness-degradation.sh — оператор-дрилл деградации readiness-пробы.
#
# Что делает:
#   Проверяет три сценария деградации /ready:
#     1. redis down  — ACM и user_api отдают 200 degraded (soft dep).
#     2. db down     — locker_api отдаёт 503 unavailable (critical dep).
#     3. mqtt down   — locker_api отдаёт 200 degraded (soft dep).
#   В каждом сценарии: поднимает стек, останавливает dep-контейнер,
#   гоняет gated pytest-тест с флагом SCUD_*_DOWN=1, возвращает dep.
#
# Это РУЧНОЙ оператор-дрилл, не CI. Gated-тесты скипаются в обычном
# docker compose --profile tests прогоне (флаг не выставлен).
#
# Запуск:
#   bash backend/scripts/test-readiness-degradation.sh
#
# Требования:
#   - Docker Engine запущен; docker compose доступен в PATH.
#   - Секреты проинициализированы: bash backend/scripts/seed-dev-secrets.sh
#   - Запускать из корня репо ИЛИ из backend/ — скрипт сам перейдёт в backend/.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "${BACKEND_DIR}"

# ── Trap: восстановить всё при любом выходе ───────────────────────────────────
STOPPED_CONTAINERS=()

# shellcheck disable=SC2317  # вызывается через trap EXIT
cleanup() {
    if [ "${#STOPPED_CONTAINERS[@]}" -gt 0 ]; then
        echo "[cleanup] Перезапуск остановленных контейнеров: ${STOPPED_CONTAINERS[*]}"
        docker compose start "${STOPPED_CONTAINERS[@]}" 2>/dev/null || true
    fi
    echo "[cleanup] docker compose down -v"
    docker compose down -v 2>/dev/null || true
}

trap cleanup EXIT

# ── Хелперы ───────────────────────────────────────────────────────────────────
FAILURES=0

pass() { echo "[PASS] $*"; }
fail() { echo "[FAIL] $*" >&2; FAILURES=$((FAILURES + 1)); }

# Удалить элемент из массива STOPPED_CONTAINERS по точному совпадению имени.
remove_stopped() {
    local target="$1"
    local new_arr=()
    for item in "${STOPPED_CONTAINERS[@]}"; do
        if [ "${item}" != "${target}" ]; then
            new_arr+=("${item}")
        fi
    done
    STOPPED_CONTAINERS=("${new_arr[@]+"${new_arr[@]}"}")
}

# Запустить gated-тест без пересборки и без перезапуска dep-контейнеров.
# Аргументы: <test_service> <env_flag=value> <test_nodeid>
run_gated() {
    local svc="$1"
    local env_flag="$2"
    local nodeid="$3"
    docker compose run --rm --no-deps -e "${env_flag}" \
        --entrypoint pytest \
        "${svc}" -q "${nodeid}"
}

# ── Сценарий 1: redis down (ACM + user_api → 200 degraded) ───────────────────
echo ""
echo "=== Сценарий 1: redis down ==="

docker compose up -d acm user_api

echo "[drill] Останавливаем redis..."
docker compose stop redis
STOPPED_CONTAINERS+=(redis)

# Даём небольшую паузу для детектирования разрыва.
sleep 2

echo "[drill] Запуск ACM redis-gated теста..."
if run_gated acm_tests "SCUD_REDIS_DOWN=1" \
    "tests/integration/test_health_probes.py::test_ready_degraded_when_redis_down"; then
    pass "redis down / ACM → 200 degraded"
else
    fail "redis down / ACM"
fi

echo "[drill] Запуск user_api redis-gated теста..."
if run_gated user_api_tests "SCUD_REDIS_DOWN=1" \
    "tests/integration/test_health_probes.py::test_ready_degraded_when_redis_down"; then
    pass "redis down / user_api → 200 degraded"
else
    fail "redis down / user_api"
fi

echo "[drill] Возобновляем redis..."
docker compose start redis
remove_stopped redis

# ── Сценарий 2: db down (locker_api → 503 unavailable) ───────────────────────
echo ""
echo "=== Сценарий 2: db down ==="

docker compose up -d locker_api

echo "[drill] Останавливаем postgres_locker..."
docker compose stop postgres_locker
STOPPED_CONTAINERS+=(postgres_locker)

# pgbouncer_locker остаётся запущенным. Он детектирует отказ postgres
# через connect_timeout (≤15 s) и начинает возвращать клиентам ошибку.
# Ожидаем, пока /ready покажет db=unavailable (до 45 s).
echo "[drill] Ожидаем, пока /ready покажет db=unavailable..."
DB_POLL=0
while [ "${DB_POLL}" -lt 45 ]; do
    sleep 2
    DB_POLL=$((DB_POLL + 2))
    DB_STATUS=$(docker compose run --rm --no-deps --entrypoint sh locker_api_tests \
        -c 'curl -sf http://locker_api:8001/ready' 2>/dev/null || true)
    if echo "${DB_STATUS}" | grep -q '"db":"unavailable"'; then
        echo "[drill] db=unavailable зафиксирован (${DB_POLL}s)"
        break
    fi
done

echo "[drill] Запуск db-gated теста..."
if run_gated locker_api_tests "SCUD_DB_DOWN=1" \
    "tests/integration/test_health_probes.py::test_ready_unavailable_when_db_down"; then
    pass "db down / locker_api → 503 unavailable"
else
    fail "db down / locker_api"
fi

echo "[drill] Возобновляем postgres_locker..."
docker compose start postgres_locker
remove_stopped postgres_locker

# ── Сценарий 3: mqtt down (locker_api → 200 degraded) ────────────────────────
echo ""
echo "=== Сценарий 3: mqtt down ==="

# locker_api уже должен быть up после сценария 2 (БД вернулась).
# Проверим и поднимем при необходимости.
docker compose up -d locker_api

echo "[drill] Останавливаем mqtt..."
docker compose stop mqtt
STOPPED_CONTAINERS+=(mqtt)

# gmqtt детектирует разрыв через heartbeat (~5 s); ждём до 20 s.
echo "[drill] Ожидаем, пока gmqtt зафиксирует разрыв..."
POLL=0
while [ "${POLL}" -lt 20 ]; do
    sleep 2
    POLL=$((POLL + 2))
    # Пробуем /ready — как только mqtt=unavailable, тест можно запускать.
    STATUS=$(docker compose run --rm --no-deps --entrypoint sh locker_api_tests \
        -c 'curl -sf http://locker_api:8001/ready' 2>/dev/null || true)
    if echo "${STATUS}" | grep -q '"mqtt":"unavailable"'; then
        echo "[drill] mqtt=unavailable зафиксирован (${POLL}s)"
        break
    fi
done

echo "[drill] Запуск mqtt-gated теста..."
if run_gated locker_api_tests "SCUD_MQTT_DOWN=1" \
    "tests/integration/test_health_probes.py::test_ready_degraded_when_mqtt_down"; then
    pass "mqtt down / locker_api → 200 degraded"
else
    fail "mqtt down / locker_api"
fi

echo "[drill] Возобновляем mqtt..."
docker compose start mqtt
remove_stopped mqtt

# ── Итог ──────────────────────────────────────────────────────────────────────
echo ""
if [ "${FAILURES}" -eq 0 ]; then
    echo "=== Все сценарии: PASS ==="
    exit 0
else
    echo "=== Провалено сценариев: ${FAILURES} — см. [FAIL] выше ===" >&2
    exit 1
fi
