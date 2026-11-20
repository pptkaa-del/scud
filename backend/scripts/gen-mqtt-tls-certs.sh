#!/usr/bin/env bash
# Генерирует собственный CA и серверный сертификат для TLS-листенера MQTT.
#
# TLS терминирует haproxy-sidecar (:8883 ssl -> mqtt:1883 внутри backend_net):
# сам брокер собран WITH_TLS=no, потому что libssl-dev нет в сертифицированном
# Astra main.
#
# Usage:
#   bash backend/scripts/gen-mqtt-tls-certs.sh [OPTIONS]
#
#   --dns NAME        SAN DNS-имя (повторяемый; заменяет весь список по умолчанию)
#   --ip ADDR         SAN IP-адрес (повторяемый; заменяет весь список по умолчанию)
#   --cn NAME         CN серверного сертификата (по умолчанию — первое DNS-имя)
#   --ca-cn NAME      CN корневого CA
#   --days N          срок серверного сертификата (по умолчанию 825)
#   --ca-days N       срок CA (по умолчанию 3650)
#   --out DIR         корень выходного дерева (по умолчанию backend/mqtt-tls)
#   --force           перевыпустить серверный сертификат поверх существующего
#   --force-ca        дополнительно перевыпустить CA (требует перепрошивки плат)
#   --chain-root      включить ca.crt в server.pem (плата такую цепочку НЕ примет)
#   --openssl MODE    docker | host | auto (по умолчанию auto)
#   --image REF       образ для MODE=docker
#   -h, --help        показать эту справку
#
# Те же значения читаются из env: MQTT_TLS_DNS, MQTT_TLS_IP, MQTT_TLS_CN,
# MQTT_TLS_CA_CN, MQTT_TLS_DAYS, MQTT_TLS_CA_DAYS, MQTT_TLS_OUT,
# MQTT_TLS_OPENSSL, MQTT_TLS_IMAGE, MQTT_TLS_PEM_MODE.
#
# Ротация в production — backend/DEPLOYMENT.md.
set -euo pipefail

REPO_BACKEND="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

OUT_DIR="${MQTT_TLS_OUT:-$REPO_BACKEND/mqtt-tls}"
SERVER_CN="${MQTT_TLS_CN:-}"
CA_CN="${MQTT_TLS_CA_CN:-SCUD Relay CA}"
DAYS="${MQTT_TLS_DAYS:-825}"
CA_DAYS="${MQTT_TLS_CA_DAYS:-10950}"
OPENSSL_MODE="${MQTT_TLS_OPENSSL:-auto}"
OPENSSL_IMAGE="${MQTT_TLS_IMAGE:-registry.astralinux.ru/library/astra/ubi18-python311:1.8.5}"
# 0600: haproxy читает `crt` на стадии разбора конфига, ещё будучи root, и
# только потом делает setuid по директиве `user` из haproxy.cfg. Это отличает
# его от postgres (см. seed-dev-secrets.sh), чей entrypoint переходит на uid 999
# ДО чтения POSTGRES_PASSWORD_FILE и потому требует 0644. Ослаблять до 0644
# нужно только если sidecar-образ объявляет USER в Dockerfile.
PEM_MODE="${MQTT_TLS_PEM_MODE:-0600}"
# Приёмный буфер платы 1024 Б, и сообщение Certificate она не умеет собирать
# из нескольких TLS-записей. Превышение = молчаливый обрыв рукопожатия.
DER_LIMIT="${MQTT_TLS_DER_LIMIT:-1000}"
CHAIN_ROOT=0
FORCE=0
FORCE_CA=0

DNS_NAMES=()
IP_ADDRS=()
DNS_OVERRIDDEN=0
IP_OVERRIDDEN=0

usage() {
    sed -n '2,${/^#/!q; s/^#\( \|$\)//; p;}' "${BASH_SOURCE[0]}"
}

die() {
    echo "gen-mqtt-tls-certs: $*" >&2
    exit 1
}

need_arg() {
    [ "$1" -ge 2 ] || die "$2 требует аргумент"
}

while [ $# -gt 0 ]; do
    case "$1" in
        --dns)
            need_arg $# "$1"
            if [ "$DNS_OVERRIDDEN" -eq 0 ]; then
                DNS_NAMES=()
                DNS_OVERRIDDEN=1
            fi
            DNS_NAMES+=("$2")
            shift 2
            ;;
        --ip)
            need_arg $# "$1"
            if [ "$IP_OVERRIDDEN" -eq 0 ]; then
                IP_ADDRS=()
                IP_OVERRIDDEN=1
            fi
            IP_ADDRS+=("$2")
            shift 2
            ;;
        --cn)      need_arg $# "$1"; SERVER_CN="$2"; shift 2 ;;
        --ca-cn)   need_arg $# "$1"; CA_CN="$2"; shift 2 ;;
        --days)    need_arg $# "$1"; DAYS="$2"; shift 2 ;;
        --ca-days) need_arg $# "$1"; CA_DAYS="$2"; shift 2 ;;
        --out)     need_arg $# "$1"; OUT_DIR="$2"; shift 2 ;;
        --openssl) need_arg $# "$1"; OPENSSL_MODE="$2"; shift 2 ;;
        --image)   need_arg $# "$1"; OPENSSL_IMAGE="$2"; shift 2 ;;
        --force)         FORCE=1; shift ;;
        --force-ca)      FORCE=1; FORCE_CA=1; shift ;;
        --chain-root)    CHAIN_ROOT=1; shift ;;
        -h|--help)       usage; exit 0 ;;
        *) die "неизвестный аргумент: $1 (см. --help)" ;;
    esac
done

if [ "$DNS_OVERRIDDEN" -eq 0 ]; then
    read -r -a DNS_NAMES <<< "${MQTT_TLS_DNS:-localhost mqtt}"
fi
if [ "$IP_OVERRIDDEN" -eq 0 ]; then
    read -r -a IP_ADDRS <<< "${MQTT_TLS_IP:-127.0.0.1}"
fi

[ "${#DNS_NAMES[@]}" -gt 0 ] || die "нужно хотя бы одно DNS-имя в SAN"
[ -n "$SERVER_CN" ] || SERVER_CN="${DNS_NAMES[0]}"
[ "${#SERVER_CN}" -le 64 ] || die "CN длиннее 64 символов: $SERVER_CN"

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"

# --- выбор openssl ----------------------------------------------------------
# Глобальное правило проекта — инструменты в Docker. На стенде и на CI-раннере
# openssl есть в системе, а тянуть образ может быть нечем, поэтому auto
# сначала пробует docker и молча откатывается на системный бинарь.
host_path() {
    if command -v cygpath > /dev/null 2>&1; then
        cygpath -m "$1"
    else
        printf '%s' "$1"
    fi
}

case "$OPENSSL_MODE" in
    docker | host) ;;
    auto)
        if command -v docker > /dev/null 2>&1 && docker info > /dev/null 2>&1; then
            OPENSSL_MODE=docker
        elif command -v openssl > /dev/null 2>&1; then
            OPENSSL_MODE=host
        else
            die "не найден ни рабочий docker, ни системный openssl"
        fi
        ;;
    *) die "--openssl принимает docker|host|auto, получено: $OPENSSL_MODE" ;;
esac

DOCKER_USER_ARGS=()
if [ "$OPENSSL_MODE" = docker ]; then
    command -v docker > /dev/null 2>&1 || die "--openssl docker, но docker не найден"
    # Без --user контейнер пишет от root, и на Linux-хосте сгенерированные файлы
    # окажутся чужими для вызвавшего пользователя. На Windows uid виртуализован.
    case "$(uname -s)" in
        Linux | Darwin) DOCKER_USER_ARGS=(--user "$(id -u):$(id -g)") ;;
    esac
else
    command -v openssl > /dev/null 2>&1 || die "--openssl host, но openssl не найден"
fi

# Все пути ниже — ОТНОСИТЕЛЬНЫЕ к $OUT_DIR: одна и та же команда работает и на
# хосте, и внутри контейнера с bind-mount на /work.
run_openssl() {
    if [ "$OPENSSL_MODE" = host ]; then
        openssl "$@"
    else
        # MSYS_NO_PATHCONV/MSYS2_ARG_CONV_EXCL: Git Bash иначе переписывает
        # `/work` в `C:/Program Files/Git/work`.
        MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL='*' \
            docker run --rm ${DOCKER_USER_ARGS[@]+"${DOCKER_USER_ARGS[@]}"} \
            -v "$(host_path "$OUT_DIR"):/work" -w /work \
            "$OPENSSL_IMAGE" openssl "$@"
    fi
}

run_openssl_quiet() {
    local err
    if ! err="$(run_openssl "$@" 2>&1)"; then
        printf '%s
' "$err" >&2
        die "openssl $1 завершился с ошибкой"
    fi
}

mkdir -p "$OUT_DIR/ca" "$OUT_DIR/server" "$OUT_DIR/certs"
cd "$OUT_DIR" || exit 1

# certs/ монтируется в haproxy read-only и содержит ТОЛЬКО server.pem: директива
# `crt` в форме каталога загружает каждый файл каталога, и одиночный server.crt
# без ключа уронил бы старт фронтенда.
CA_KEY=ca/ca.key
CA_CRT=ca/ca.crt
CA_SRL=ca/ca.srl
SRV_KEY=server/server.key
SRV_CRT=server/server.crt
SRV_CSR=server/server.csr
SRV_PEM=certs/server.pem

write_ca_cnf() {
    cat > ca/ca.cnf <<CNF
[req]
default_md = sha256
distinguished_name = dn
prompt = no
x509_extensions = v3_ca

[dn]
CN = $CA_CN

[v3_ca]
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always,issuer
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
CNF
}

write_server_cnf() {
    {
        cat <<CNF
[req]
default_md = sha256
distinguished_name = dn
prompt = no

[dn]
CN = $SERVER_CN

[v3_server]
subjectAltName = @alt_names

[alt_names]
CNF
        idx=0
        for name in "${DNS_NAMES[@]}"; do
            idx=$((idx + 1))
            printf 'DNS.%d = %s\n' "$idx" "$name"
        done
        idx=0
        for addr in "${IP_ADDRS[@]}"; do
            idx=$((idx + 1))
            printf 'IP.%d = %s\n' "$idx" "$addr"
        done
    } > server/server.cnf
}

# --- CA ---------------------------------------------------------------------
# Ключ CA положено хранить офлайн, поэтому его отсутствие при живом ca.crt —
# штатная ситуация, а не повод выпустить новый корень: это отключило бы все
# прошитые платы. Требуем вернуть ключ или подтвердить смену CA явно.
if [ -s "$CA_CRT" ] && [ ! -s "$CA_KEY" ] && [ "$FORCE_CA" -eq 0 ]; then
    echo "$OUT_DIR/$CA_CRT есть, а $CA_KEY нет." >&2
    echo "Верните ключ CA из офлайн-хранилища — он нужен для подписи нового" >&2
    echo "серверного сертификата. Выпуск нового корня (--force-ca) потребует" >&2
    echo "перепрошивки КАЖДОЙ платы." >&2
    exit 1
fi

if [ -s "$CA_CRT" ] && [ -s "$CA_KEY" ] && [ "$FORCE_CA" -eq 0 ]; then
    echo "skip CA (уже существует: $OUT_DIR/$CA_CRT)"
else
    if [ -s "$CA_CRT" ]; then
        echo "ВНИМАНИЕ: --force-ca перевыпускает корневой CA. Платы, в которые" >&2
        echo "          залит старый ca.crt, не подключатся до перепрошивки." >&2
    fi
    write_ca_cnf
    run_openssl_quiet req -x509 -new -nodes -newkey rsa:2048 -sha256 \
        -days "$CA_DAYS" -config ca/ca.cnf -extensions v3_ca \
        -keyout "$CA_KEY" -out "$CA_CRT"
    rm -f "$CA_SRL"
    echo "generated CA     -> $OUT_DIR/$CA_CRT"
fi

# --- серверный сертификат ---------------------------------------------------
if [ -s "$SRV_PEM" ] && [ "$FORCE" -eq 0 ]; then
    echo "skip server (уже существует: $OUT_DIR/$SRV_PEM; --force для перевыпуска)"
else
    write_server_cnf
    # CSR намеренно без req_extensions: `x509 -req` игнорирует расширения запроса
    # без -copy_extensions, а authorityKeyIdentifier в CSR вообще невыразим.
    # Единственный источник расширений — -extfile на шаге подписи.
    run_openssl_quiet req -new -nodes -newkey rsa:2048 -sha256 \
        -config server/server.cnf -keyout "$SRV_KEY" -out "$SRV_CSR"
    run_openssl_quiet x509 -req -in "$SRV_CSR" -sha256 -days "$DAYS" \
        -CA "$CA_CRT" -CAkey "$CA_KEY" -CAcreateserial -CAserial "$CA_SRL" \
        -extfile server/server.cnf -extensions v3_server \
        -out "$SRV_CRT"
    # Порядок для haproxy `crt`: лист -> цепочка -> приватный ключ. Корневой CA
    # по умолчанию НЕ включается: прошивка платы принимает ровно один сертификат
    # в цепочке, второй не влезает в её приёмный буфер и рвёт рукопожатие.
    if [ "$CHAIN_ROOT" -eq 1 ]; then
        cat "$SRV_CRT" "$CA_CRT" "$SRV_KEY" > "$SRV_PEM"
    else
        cat "$SRV_CRT" "$SRV_KEY" > "$SRV_PEM"
    fi
    echo "generated server -> $OUT_DIR/$SRV_PEM"
fi

chmod 0700 ca server
chmod 0755 certs
chmod 0600 "$CA_KEY" "$SRV_KEY"
chmod 0644 "$CA_CRT" "$SRV_CRT"
chmod "$PEM_MODE" "$SRV_PEM"

run_openssl verify -CAfile "$CA_CRT" "$SRV_CRT" > /dev/null \
    || die "самопроверка не прошла: server.crt не проверяется против ca.crt"

run_openssl x509 -in "$SRV_CRT" -outform DER -out server/server.der
DER_SIZE=$(wc -c < server/server.der)
rm -f server/server.der
if [ "$DER_SIZE" -gt "$DER_LIMIT" ]; then
    echo "server.crt: $DER_SIZE Б в DER при лимите $DER_LIMIT Б" >&2
    echo "Плата с таким сертификатом не подключится. Сокращать в порядке эффекта:" >&2
    echo "CN (~2 Б за символ — идёт и в subject, и в SAN), лишние DNS-имена, лишние IP." >&2
    exit 1
fi

CHAIN_LEN=$(grep -c "BEGIN CERTIFICATE" "$SRV_PEM")
[ "$CHAIN_LEN" -eq 1 ] || die "в $SRV_PEM сертификатов: $CHAIN_LEN, плата примет только один"


echo
run_openssl x509 -in "$CA_CRT" -noout -fingerprint -sha256 | sed 's/^/CA  /'
run_openssl x509 -in "$SRV_CRT" -noout -subject -dates -ext subjectAltName \
    | sed 's/^/SRV /'
echo
echo "DER      $DER_SIZE Б из $DER_LIMIT (запас $((DER_LIMIT - DER_SIZE)) Б), в цепочке: $CHAIN_LEN"
echo
echo "haproxy: bind :8883 ssl crt /etc/haproxy/certs/server.pem"
echo "mount:   \${MQTT_TLS_CERT_DIR:-./mqtt-tls/certs}:/etc/haproxy/certs:ro"
echo "в плату: $OUT_DIR/$CA_CRT"
