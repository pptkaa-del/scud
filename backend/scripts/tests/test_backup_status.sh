#!/bin/bash
# Тесты логики записи статус-файла в backup.sh.
# Запуск внутри Docker:
#   docker run --rm -v <repo>/backend:/backend bash:5 \
#     bash /backend/scripts/tests/test_backup_status.sh
#
# Стратегия: для каждого теста создаём временный BACKUP_DIR, подставляем
# MOCK-реализацию pg_dump на PATH (POSIX sh — работает без /bin/bash),
# запускаем backup.sh через `bash` и проверяем статус-файл.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKUP_SH="$SCRIPT_DIR/../backup.sh"

pass=0
fail=0

ok()  { echo "  PASS: $1"; pass=$((pass + 1)); }
err() { echo "  FAIL: $1" >&2; fail=$((fail + 1)); }

tmpdir() { mktemp -d; }

# ---------------------------------------------------------------------------
# Тест 1: все pg_dump успешны → статус-файл содержит OK, скрипт выходит 0.
# ---------------------------------------------------------------------------
echo "==> Тест 1: все дампы успешны"
TDIR=$(tmpdir)
MOCK_BIN="$TDIR/bin"
mkdir -p "$MOCK_BIN"

# Мок pg_dump на POSIX sh — работает в Alpine (bash:5 = Alpine + bash).
cat > "$MOCK_BIN/pg_dump" << 'MOCK'
#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-f" ]; then
        touch "$2"
        break
    fi
    shift
done
exit 0
MOCK
chmod +x "$MOCK_BIN/pg_dump"

BACKUP_DIR_T="$TDIR/backups"
if PGPASSWORD=testpw BACKUP_DIR="$BACKUP_DIR_T" PATH="$MOCK_BIN:$PATH" bash "$BACKUP_SH"; then
    status=$(cat "$BACKUP_DIR_T/last_run_status" 2>/dev/null || true)
    if printf '%s' "$status" | grep -q "^OK "; then
        ok "статус-файл начинается с OK"
    else
        err "статус-файл не содержит OK: '$status'"
    fi
else
    err "backup.sh завершился с ненулевым кодом при успехе всех дампов"
fi
rm -rf "$TDIR"

# ---------------------------------------------------------------------------
# Тест 2: один pg_dump завершается с ошибкой → статус-файл FAIL <db>, код 2.
# ---------------------------------------------------------------------------
echo "==> Тест 2: один дамп завершается с ошибкой (scud_lockers)"
TDIR=$(tmpdir)
MOCK_BIN="$TDIR/bin"
mkdir -p "$MOCK_BIN"

cat > "$MOCK_BIN/pg_dump" << 'MOCK'
#!/bin/sh
db=""
out=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -d) db="$2"; shift ;;
        -f) out="$2"; shift ;;
    esac
    shift
done
if [ "$db" = "scud_lockers" ]; then
    exit 1
fi
[ -n "$out" ] && touch "$out"
exit 0
MOCK
chmod +x "$MOCK_BIN/pg_dump"

BACKUP_DIR_T="$TDIR/backups"
exit_code=0
PGPASSWORD=testpw BACKUP_DIR="$BACKUP_DIR_T" PATH="$MOCK_BIN:$PATH" bash "$BACKUP_SH" || exit_code=$?

if [ "$exit_code" -eq 2 ]; then
    ok "скрипт завершился с кодом 2"
else
    err "ожидался код выхода 2, получен $exit_code"
fi

status=$(cat "$BACKUP_DIR_T/last_run_status" 2>/dev/null || true)
if printf '%s' "$status" | grep -q "^FAIL "; then
    ok "статус-файл начинается с FAIL"
else
    err "статус-файл не содержит FAIL: '$status'"
fi
if printf '%s' "$status" | grep -q "scud_lockers"; then
    ok "статус-файл упоминает имя упавшей БД"
else
    err "статус-файл не упоминает scud_lockers: '$status'"
fi
rm -rf "$TDIR"

# ---------------------------------------------------------------------------
# Тест 3: отсутствует секрет и PGPASSWORD не задан → FAIL, код 2.
# ---------------------------------------------------------------------------
echo "==> Тест 3: секрет отсутствует, PGPASSWORD не задан"
TDIR=$(tmpdir)
MOCK_BIN="$TDIR/bin"
mkdir -p "$MOCK_BIN"

cat > "$MOCK_BIN/pg_dump" << 'MOCK'
#!/bin/sh
while [ "$#" -gt 0 ]; do
    if [ "$1" = "-f" ]; then touch "$2"; break; fi
    shift
done
exit 0
MOCK
chmod +x "$MOCK_BIN/pg_dump"

BACKUP_DIR_T="$TDIR/backups"
exit_code=0
# Убрать PGPASSWORD из среды: без него и без secret-файлов скрипт должен упасть.
unset PGPASSWORD || true
BACKUP_DIR="$BACKUP_DIR_T" PATH="$MOCK_BIN:$PATH" bash "$BACKUP_SH" || exit_code=$?

if [ "$exit_code" -eq 2 ]; then
    ok "скрипт завершился с кодом 2 при отсутствии секретов"
else
    err "ожидался код 2, получен $exit_code"
fi

status=$(cat "$BACKUP_DIR_T/last_run_status" 2>/dev/null || true)
if printf '%s' "$status" | grep -q "^FAIL "; then
    ok "статус-файл содержит FAIL при отсутствии секретов"
else
    err "статус-файл не содержит FAIL: '$status'"
fi
rm -rf "$TDIR"

# ---------------------------------------------------------------------------
# Итого
# ---------------------------------------------------------------------------
echo ""
echo "Результат: $pass пройдено, $fail провалено."
if [ "$fail" -gt 0 ]; then
    exit 1
fi
