#!/usr/bin/env bash
# Быстрое развёртывание СКУД на сервере из git-репозитория.
#
#   curl -fsSL https://raw.githubusercontent.com/pptkaa-del/scud/main/install.sh -o install.sh
#   sudo bash install.sh [git-url] [ref] [опции deploy.sh]
#
# Первый запуск: ставит git, готовит SSH deploy key (для приватного репозитория),
# клонирует код в /opt/scud и запускает backend/scripts/deploy.sh install.
# Повторный запуск: deploy.sh update (дамп БД, новые образы, миграции, перезапуск).
#
# Переменные: SCUD_GIT_URL, SCUD_REF (main), SCUD_DIR (/opt/scud),
# SCUD_DEPLOY_KEY (/root/.ssh/scud_deploy). Все SCUD_* из deploy.sh -h тоже работают,
# например: SCUD_PUBLIC_URL=https://scud.example.com sudo -E bash install.sh URL main -y
set -euo pipefail

REPO=${1:-${SCUD_GIT_URL:-https://github.com/pptkaa-del/scud.git}}
REF=${2:-${SCUD_REF:-main}}
[ "$#" -gt 0 ] && shift
[ "$#" -gt 0 ] && shift
DIR=${SCUD_DIR:-/opt/scud}
KEY=${SCUD_DEPLOY_KEY:-/root/.ssh/scud_deploy}

info() { printf '==> %s\n' "$*"; }
die() {
    printf 'ОШИБКА: %s\n' "$*" >&2
    exit 1
}

[ -n "$REPO" ] || die "укажите URL репозитория: sudo bash install.sh git@github.com:OWNER/REPO.git [ref]"
[ "$(id -u)" -eq 0 ] || die "запустите от root: sudo bash install.sh ..."

# --- Зависимости ---
missing=()
for c in git curl openssl ssh; do
    command -v "$c" >/dev/null 2>&1 || missing+=("$c")
done
if [ "${#missing[@]}" -gt 0 ]; then
    command -v apt-get >/dev/null 2>&1 || die "нет ${missing[*]}; установите вручную"
    info "Установка пакетов: ${missing[*]}"
    pkgs=()
    for c in "${missing[@]}"; do
        case "$c" in
            ssh) pkgs+=(openssh-client) ;;
            *) pkgs+=("$c") ;;
        esac
    done
    apt-get update -qq && apt-get install -y -qq "${pkgs[@]}"
fi

# --- SSH deploy key для приватного репозитория ---
case "$REPO" in
    git@* | ssh://*)
        host=${REPO#ssh://}
        host=${host#*@}
        host=${host%%[:/]*}
        mkdir -p /root/.ssh && chmod 700 /root/.ssh
        if [ ! -f "$KEY" ]; then
            info "Создание deploy key $KEY"
            ssh-keygen -q -t ed25519 -N '' -C "scud-deploy@$(hostname)" -f "$KEY"
        fi
        if ! ssh-keygen -F "$host" -f /root/.ssh/known_hosts >/dev/null 2>&1; then
            ssh-keyscan -t ed25519,rsa "$host" >>/root/.ssh/known_hosts 2>/dev/null
        fi
        export GIT_SSH_COMMAND="ssh -i $KEY -o IdentitiesOnly=yes"
        if ! git ls-remote -q "$REPO" HEAD >/dev/null 2>&1; then
            printf '\nНет доступа к %s.\n' "$REPO" >&2
            printf 'Добавьте этот ключ в GitHub: репозиторий -> Settings -> Deploy keys -> Add deploy key\n' >&2
            printf '(галочку «Allow write access» не ставить), затем запустите install.sh ещё раз:\n\n' >&2
            cat "$KEY.pub" >&2
            printf '\n' >&2
            exit 1
        fi
        ;;
    *)
        git ls-remote -q "$REPO" HEAD >/dev/null 2>&1 || die "нет доступа к $REPO"
        ;;
esac

# --- Код ---
if [ ! -d "$DIR/.git" ]; then
    if [ -d "$DIR" ] && [ -n "$(ls -A "$DIR" 2>/dev/null)" ]; then
        die "каталог $DIR не пуст и не git-клон; задайте другой SCUD_DIR"
    fi
    info "git clone $REPO -> $DIR"
    git clone -q "$REPO" "$DIR"
fi
if [ -n "${GIT_SSH_COMMAND:-}" ]; then
    # update в deploy.sh делает git fetch — ключ нужен и ему
    git -C "$DIR" config core.sshCommand "$GIT_SSH_COMMAND"
fi
chmod +x "$DIR/backend/scripts/deploy.sh"

# --- Установка или обновление ---
if [ -f "$DIR/backend/deploy.conf" ]; then
    info "Найдена установка в $DIR — обновление до $REF"
    exec "$DIR/backend/scripts/deploy.sh" update --dir "$DIR" --ref "$REF" "$@"
else
    info "Новая установка в $DIR ($REF)"
    exec "$DIR/backend/scripts/deploy.sh" install --dir "$DIR" --git "$REPO" --ref "$REF" "$@"
fi
