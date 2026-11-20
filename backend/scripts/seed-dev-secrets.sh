#!/usr/bin/env bash
# Создаёт dev-default secret-файлы в backend/secrets/, если их там ещё нет.
#
# Используется в CI и при первом локальном запуске:
# - postgres_user читает пароль через POSTGRES_PASSWORD_FILE; docker compose
#   secrets: file: бинды требуют существующие файлы.
# - Реальные secret-файлы в .gitignore, поэтому свежий clone их не имеет.
#
# Production: НЕ использовать. Для prod секреты генерируются через
# `openssl rand` (см. backend/DEPLOYMENT.md → "First-time deployment").
#
# Usage: bash backend/scripts/seed-dev-secrets.sh
set -euo pipefail

SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/secrets"
mkdir -p "$SECRETS_DIR"

# (имя_файла, dev-default содержимое)
seed() {
    local name="$1"
    local value="$2"
    local path="$SECRETS_DIR/$name"
    if [ -f "$path" ] && [ -s "$path" ]; then
        # Если уже существует, всё равно поправим права на 0644 — может
        # остаться 0600 от старой версии скрипта, что ломает чтение из
        # postgres-контейнера (см. ниже).
        chmod 0644 "$path"
        echo "skip $name (already exists, non-empty)"
        return
    fi
    # Edge case (Windows + старые версии docker): bind-mount к
    # несуществующему файлу мог создать на его месте директорию. Удалим,
    # чтобы корректно записать файл.
    if [ -d "$path" ]; then
        rmdir "$path" 2>/dev/null || rm -rf "$path"
    fi
    printf '%s' "$value" > "$path"
    # 0644 (а не 0600) — bind-mount сохраняет uid файла на стороне хоста
    # (uid пользователя, запустившего CI/dev). Postgres docker-entrypoint
    # запускает `_load_password_file` УЖЕ от postgres-юзера (uid 999 в
    # контейнере), а не от root, поэтому 0600 на файле от runner uid =
    # «Permission denied». В DEV-окружении (где значения — `password`,
    # `changeme` и т.п.) разрешаем world-read; для production
    # `backend/DEPLOYMENT.md` инструктирует генерировать секреты с
    # 0600 + правильным ownership.
    chmod 0644 "$path"
    echo "seeded $name"
}

# postgres_<svc>_password: пароль admin-суперпользователя КАЖДОГО из 6
# postgres-инстансов — отдельный секрет на инстанс (SEC-24). Утечка пароля
# одного сервиса больше НЕ даёт доступ к остальным 5 БД. dev-значение у всех
# одинаковое ("password") для удобства; prod генерирует разные через
# `openssl rand` (см. DEPLOYMENT.md).
seed "postgres_user_password" "password"
seed "postgres_locker_password" "password"
seed "postgres_history_password" "password"
seed "postgres_auth_password" "password"
seed "postgres_notification_password" "password"
seed "postgres_reports_password" "password"
seed "redis_password" "redis-dev-only-do-not-use-in-prod"
seed "admin_password" "changeme"
seed "jwt_secret" "change-me-in-production-must-be-at-least-32-bytes-long"
seed "internal_auth_hmac_key" "internal-auth-dev-key-do-not-use-in-prod"
seed "internal_otp_hmac_key" "internal-otp-dev-key-do-not-use-in-prod"
seed "internal_max_binding_hmac_key" "internal-max-binding-dev-key-do-not-use-in-prod"
seed "internal_user_read_hmac_key" "internal-user-read-dev-key-do-not-use-in-prod"
seed "internal_session_revoke_hmac_key" "internal-session-revoke-dev-key-do-not-use-in-prod"
seed "internal_step_up_hmac_key" "internal-step-up-dev-key-do-not-use-in-prod"
seed "internal_history_hmac_key" "internal-history-dev-key-do-not-use-in-prod"
# acm_db_password: пароль least-privilege роли acm_ro (SELECT-only) в scud_users.
# postgres_user init-скрипт создаёт роль из этого секрета; ACM читает тот же секрет.
# Prod: ротация — см. backend/DEPLOYMENT.md.
seed "acm_db_password" "acm_ro_dev_password"
# pgb_admin_password: пароль локальной admin/stats-роли pgb_admin КАЖДОГО
# из 6 PgBouncer-пулеров (psql -p 6432 -U pgb_admin -d pgbouncer → SHOW
# STATS/POOLS). Один секрет, два читателя: pgbouncer-entrypoint пишет его в
# userlist.txt, zabbix_agent2 (профиль monitoring) им же логинится в
# admin-консоль для infra-метрик. Prod: ротация — см. DEPLOYMENT.md.
seed "pgb_admin_password" "pgb_admin_dev_password"
# postgres_zabbix_password: пароль admin-суперпользователя postgres_zabbix
# (POSTGRES_PASSWORD_FILE). Prod: openssl rand — см. DEPLOYMENT.md.
seed "postgres_zabbix_password" "password"
# zabbix_db_password: пароль роли-владельца БД Zabbix (профиль monitoring).
# postgres_zabbix init-скрипт создаёт роль из этого секрета; zabbix_server и
# zabbix_web читают тот же файл. Prod: openssl rand — см. DEPLOYMENT.md.
seed "zabbix_db_password" "zabbix_dev_password"
# mqtt_password: пароль backend-пользователя scud_backend в MQTT-брокере.
# Брокер пишет passwordfile из этого секрета при старте; locker_api читает тот же
# секрет (read_secret). dev-default совпадает с env-fallback в docker-compose,
# чтобы relay-интеграционные тесты подключались к брокеру без отдельной настройки.
# Prod: ротация — см. backend/DEPLOYMENT.md → "Ротация MQTT-пароля брокера".
seed "mqtt_password" "scud_backend_dev_password"
# max_bot_token: для боевого бота — токен из @MasterBot. Пустой dev-default
# означает «Max не подключён»: notification_api запускается, /internal/api/v1/otp/send
# отвечает 200 queued_until_binding (см. internal_otp.py).
seed "max_bot_token" ""
# sms_api_user / sms_api_password: креды Yeastar TG200 HTTP API. wg_private_key /
# wg_preshared_key: ключи WireGuard-туннеля до шлюза (profile sms). Пустые
# dev-default — реальные значения только на стенде (SKYD_init.conf).
seed "sms_api_user" ""
seed "sms_api_password" ""
seed "wg_private_key" ""
seed "wg_preshared_key" ""

echo "Dev secrets ready in $SECRETS_DIR"
