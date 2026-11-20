# mqtt-tls — TLS-терминатор MQTT

Релейная плата по протоколу (§4) подключается к брокеру по доменному имени либо
на `1883` без TLS, либо на `8883` с TLS 1.2. Брокер `scud-mqtt` собран
`WITH_TLS=no`, потому что `libssl-dev` отсутствует в сертифицированном Astra
`main`, поэтому TLS вынесен в отдельный sidecar на haproxy.

```
плата --TLS1.2:8883--> haproxy (mqtt_tls) --plaintext--> mqtt:1883 (backend_net)
```

PROXY protocol не используется: mosquitto 2.0.23 его не поддерживает. Реальный
IP TLS-платы берётся из `option tcplog` в логе `mqtt_tls`, атрибуция в логе
брокера — по username `relay-{id}`.

## Сертификация пакета

`haproxy 2.6.23-1astra6` лежит в сертифицированном `main`. Проверка:

```bash
docker run --rm registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 \
    sh -c "sed -i '/extended/d' /etc/apt/sources.list; apt-get update -qq; apt-cache policy haproxy"
# Candidate: 2.6.23-1astra6, источник .../1.8.5/main-repository
```

`available_packets.txt` — неполный снимок (нет ни `haproxy`, ни `apache2`),
поэтому предполётной проверкой для пакетов, отсутствующих в нём, считается
`apt-cache policy` в базовом образе с вырезанным extended.

## Сертификаты

```bash
bash backend/scripts/gen-mqtt-tls-certs.sh --dns <имя> --ip <адрес> --ip 127.0.0.1
```

Собственный CA (10 лет) + серверный лист (825 дней). В `certs/` попадает ТОЛЬКО
`server.pem` (лист + CA + приватный ключ одним файлом — формат, которого требует
директива `crt`); `ca/ca.key` не монтируется никуда. В плату заливается
`ca/ca.crt`. Перевыпуск листа под тем же CA (`--force`) плату не трогает;
`--force-ca` требует перепрошивки всех плат.

## Запуск

```bash
docker compose --profile mqtt-tls up -d --build mqtt_tls
```

Обычный `docker compose up` sidecar НЕ поднимает: без `certs/server.pem`
haproxy уходит в crash-loop.

## Проверка

```bash
docker compose --profile tests build mqtt_tls_tests
docker compose --profile tests run --rm mqtt_tls_tests
openssl s_client -connect <host>:8883 -servername <host> -tls1_2 -CAfile backend/mqtt-tls/ca/ca.crt </dev/null
```

## Таймауты

`timeout client/server/tunnel = 600s` при keepAlive платы 95 с. Значение ниже
~143 с приводит к тому, что haproxy молча рвёт простаивающую сессию между
кадрами `stat`, и это выглядит как «плата отваливается сама».

## Известные ограничения

- Набор `ssl-default-bind-ciphers` намеренно широкий, включая статический RSA
  и CBC-SHA1: TLS-стек платы неизвестен. После первого подключения реальной
  платы посмотреть согласованную сюиту в `option tcplog` и урезать список.
- `/metrics` (promex) не включён — sidecar пока вне Zabbix-профиля.
