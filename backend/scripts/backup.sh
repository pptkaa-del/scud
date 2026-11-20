#!/bin/bash
# ScudDocs — Postgres backup script (sidecar container).
#
# дампит все 6 баз scud_* через pg_dump в custom format (-F c),
# складывает в /backups (volume backup_data), удаляет файлы старше
# BACKUP_RETENTION_DAYS дней.
#
# Запускается:
#   - автоматически из контейнера `backup` (sleep-loop, см. Dockerfile.backup),
#   - вручную: `docker compose --profile backup run --rm backup /scripts/backup.sh`.
#
# Параметры (env):
#   BACKUP_DIR              — куда складывать (default: /backups).
#   BACKUP_RETENTION_DAYS   — TTL дампов в днях (default: 7).
#   PGPASSWORD              — пароль admin'а к postgres. SEC-24: у каждого
#                              из 6 инстансов СВОЙ admin-секрет, читается из
#                              /run/secrets/postgres_<svc>_password. Если
#                              секрет-файл отсутствует, fallback на env
#                              PGPASSWORD (общий) — для запуска вне Docker.
#
# Имена хостов postgres_* и баз scud_* совпадают с docker-compose.yaml.

set -euo pipefail

TS=$(date -u +%Y%m%d-%H%M%S)
BACKUP_DIR=${BACKUP_DIR:-/backups}
RETENTION_DAYS=${BACKUP_RETENTION_DAYS:-7}

mkdir -p "$BACKUP_DIR"

# Общий env-пароль (запуск вне Docker, один пароль на все инстансы). Сохраняем
# отдельно, чтобы он не «затирался» паролем из secret-файла предыдущей итерации.
ENV_PGPASSWORD=${PGPASSWORD:-}

# host:db:secret triples (host = имя сервиса в docker-compose.yaml;
# secret = имя файла в /run/secrets — отдельный admin-пароль на инстанс).
TRIPLES=(
    "postgres_user:scud_users:postgres_user_password"
    "postgres_locker:scud_lockers:postgres_locker_password"
    "postgres_history:scud_history:postgres_history_password"
    "postgres_auth:scud_auth:postgres_auth_password"
    "postgres_notification:scud_notifications:postgres_notification_password"
    "postgres_reports:scud_reports:postgres_reports_password"
)

failed=0
FAILED_DBS=""
for triple in "${TRIPLES[@]}"; do
    HOST=${triple%%:*}
    rest=${triple#*:}
    DB=${rest%%:*}
    SECRET=${rest#*:}
    SECRET_FILE="/run/secrets/$SECRET"
    # Каждый инстанс под своим секретом: читаем его файл в PGPASSWORD.
    # Если файла нет, но PGPASSWORD задан из env — используем его (запуск
    # вне Docker с одним общим паролем).
    if [ -f "$SECRET_FILE" ]; then
        PGPASSWORD=$(cat "$SECRET_FILE")
        export PGPASSWORD
    elif [ -n "$ENV_PGPASSWORD" ]; then
        PGPASSWORD=$ENV_PGPASSWORD
        export PGPASSWORD
    else
        echo "[$(date -u +%FT%TZ)] ERROR: secret $SECRET_FILE отсутствует и PGPASSWORD не задан" >&2
        failed=$((failed + 1))
        FAILED_DBS="$FAILED_DBS $DB"
        continue
    fi
    OUT="$BACKUP_DIR/${DB}-${TS}.dump"
    echo "[$(date -u +%FT%TZ)] Dumping $DB from $HOST -> $OUT"
    if pg_dump -h "$HOST" -U admin -d "$DB" -F c -f "$OUT"; then
        size=$(stat -c%s "$OUT" 2>/dev/null || wc -c < "$OUT")
        echo "[$(date -u +%FT%TZ)] OK $DB ($size bytes)"
    else
        echo "[$(date -u +%FT%TZ)] FAIL $DB" >&2
        rm -f "$OUT"
        failed=$((failed + 1))
        FAILED_DBS="$FAILED_DBS $DB"
    fi
done

# Rotation: удалить дампы старше RETENTION_DAYS суток.
find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' -mtime "+${RETENTION_DAYS}" -delete

count=$(find "$BACKUP_DIR" -maxdepth 1 -name '*.dump' | wc -l)
echo "[$(date -u +%FT%TZ)] Backup cycle complete. Failures: $failed. Retained dumps: $count."

STATUS_FILE="$BACKUP_DIR/last_run_status"
if [ "$failed" -gt 0 ]; then
    # Убрать ведущий пробел из списка упавших баз.
    printf 'FAIL %s %s\n' "${FAILED_DBS# }" "$(date -u +%FT%TZ)" > "$STATUS_FILE"
    exit 2
fi
printf 'OK %s\n' "$(date -u +%FT%TZ)" > "$STATUS_FILE"
