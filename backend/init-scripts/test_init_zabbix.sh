#!/usr/bin/env bash
# Smoke-тест скрипта инициализации БД Zabbix и entrypoint zabbix_server.
# Запуск: bash backend/init-scripts/test_init_zabbix.sh (из корня репозитория).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${HERE}/../.." && pwd)"
S="${HERE}/init-zabbix-db.sh"
E="${REPO_ROOT}/monitoring/zabbix-server/entrypoint.sh"

# 1. Файл существует.
test -f "${S}" || { echo "FAIL: init-zabbix-db.sh missing"; exit 1; }
# 2. Синтаксис bash.
bash -n "${S}"
# 3. Создание LOGIN-роли zabbix.
grep -q "CREATE ROLE zabbix" "${S}" || { echo "FAIL: no zabbix role"; exit 1; }
# 4. psql в строгом режиме ON_ERROR_STOP.
grep -q "ON_ERROR_STOP=1" "${S}"    || { echo "FAIL: psql not strict"; exit 1; }
# 5. Пароль параметризован через :'zbx_pw' (не конкатенация в SQL).
grep -q ":'zbx_pw'" "${S}"          || { echo "FAIL: password not parameterized"; exit 1; }

# 6. Загрузка схемы — в entrypoint.sh (файлы .gz в образе zabbix_server,
#    недоступны в postgres_zabbix; см. monitoring/CERT_GATE.md).
test -f "${E}" || { echo "FAIL: entrypoint.sh missing"; exit 1; }
bash -n "${E}"
# Схема грузится циклом: for part in schema images data; do zcat ${part}.sql.gz ...
# Проверяем, что все три части упомянуты в списке цикла и что zcat используется.
grep -q "schema" "${E}"          || { echo "FAIL: schema not referenced in entrypoint"; exit 1; }
grep -qE "images" "${E}"         || { echo "FAIL: images not referenced in entrypoint"; exit 1; }
grep -q "data" "${E}"            || { echo "FAIL: data not referenced in entrypoint"; exit 1; }
grep -q "zcat" "${E}"            || { echo "FAIL: zcat not used for schema load in entrypoint"; exit 1; }
grep -q "ON_ERROR_STOP=1" "${E}" || { echo "FAIL: psql not strict in entrypoint"; exit 1; }
# Порядок schema -> images -> data: в строке for-цикла schema должна
# стоять левее data (сравниваем по символьному смещению в строке).
FOR_LINE=$(grep "for part in" "${E}" | head -1)
[ -n "${FOR_LINE}" ] \
    || { echo "FAIL: for-loop not found in entrypoint"; exit 1; }
SCHEMA_IDX=$(echo "${FOR_LINE}" | awk '{print index($0,"schema")}')
DATA_IDX=$(echo "${FOR_LINE}"   | awk '{print index($0,"data")}')
if ! { [ "${SCHEMA_IDX}" -gt 0 ] && [ "${DATA_IDX}" -gt 0 ]; }; then
    echo "FAIL: schema/data not both in for-loop line"; exit 1
fi
[ "${SCHEMA_IDX}" -lt "${DATA_IDX}" ] \
    || { echo "FAIL: schema must come before data in for-loop"; exit 1; }

echo "OK init-zabbix-db.sh + entrypoint.sh"
