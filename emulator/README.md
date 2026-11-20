# SCUD Relay-Board Device Emulator

Headless-двойник релейной платы для системы ScudDocs. Говорит по **бинарному
MQTT-протоколу** (`com`/`stat`/`rep`/`photos`), аутентифицируется на брокере как
`relay-{id}` и воспроизводит поведение реальной прошивки — используется для
разработки, интеграционного и нагрузочного тестирования MQTT-тракта.

> Транспорт устройств в системе — **только MQTT** (релейные платы). Инструмент
> вне сертификации Astra: зависимости ставятся из PyPI (pip).

## Архитектура

```
emulator/
├── src/
│   ├── scud_relay/        # Headless-двойник релейной платы (MQTT, бинарный протокол)
│   └── scud_cli/          # CLI-обёртка (Typer): `scud relay run`
├── reference/             # golden_vectors.py + protocol_structures.h (сверка байт-в-байт)
└── tests/                 # Юнит-тесты relay-двойника (test_relay_*.py)
```

## Релейные платы (MQTT)

`scud_relay` строго сверен с оригиналом `SCUDDEV/RelayEmulator`: общие
golden-байт-векторы (`reference/golden_vectors.py`) + завендоренный
`reference/protocol_structures.h`; деривация broker-кредов
(`sha256("relay-{id}")[:16].hex()`) и поведение (relay on/off/reboot, lock
open/close, SEND_STATUS, NOT_SUPPORTED, retained stat) воспроизводят прошивку.

Транспорт — **gmqtt** (обычная pip-зависимость), не aiomqtt. Бэкенд (под
ограничениями сертификации Astra) вендорит свою копию gmqtt отдельно.

## Установка

```bash
cd emulator
python -m venv .venv
source .venv/bin/activate   # или .venv\Scripts\activate на Windows
pip install -e ".[all]"
```

## Запуск

```bash
# Один relay-двойник до локального брокера
scud relay run --count 1 --host localhost --port 1883 --base-id 5001 --version 2

# Флот из 100 устройств с ramp-up 60 с (сглаживает connect thundering herd)
scud relay run --count 100 --host localhost --port 1883 --base-id 5001 --ramp-up 60
```

Каждое устройство аутентифицируется на брокере как `relay-{id}` (id = `base-id`,
`base-id+1`, …). Полный цикл проверен на живом стеке: API создаёт+провижионит
mqtt-шкаф → двойник коннектится (per-device auth) → публикует `stat` → шкаф
online → команда `open` → `com/{id}` → двойник → `rep` → команда `executed`.

Перед подключением двойника соответствующий шкаф должен быть создан и
провижионен на backend (`POST /api/v1/lockers` с `transport=mqtt` и совпадающим
`relay_id`), чтобы брокер знал креды `relay-{id}`.

## Провижининг шкафов с адресами

`scud relay provision` создаёт шкафы в backend прямо из эмулятора: логинится в
gateway и шлёт `POST /api/v1/lockers` для каждого `relay_id`, проставляя
сгенерированный `location_address` + координаты (`location_lat`/`location_lon`),
чтобы шкафы отображались на карте, а не пустыми строками. Адреса
детерминированы по индексу флота (разные улицы/дома/города).

```bash
# 100 шкафов с адресами, relay ids 5001..5100 (аккаунт должен быть
# administrator/operator и БЕЗ 2FA — провижининг логинится паролем)
scud relay provision --count 100 --base-id 5001 \
    --gateway-url http://localhost \
    --phone +79000000000 --password <admin_pass>

# затем поднять для них флот двойников:
scud relay run --count 100 --base-id 5001
```

Повторный провижининг тех же `relay_id` идемпотентен (дубликат → `409` →
пропуск). Вместо `--password` можно передать `--access-token <cookie>`. Все опции
читаются и из env: `SCUD_GATEWAY_URL`, `SCUD_ADMIN_PHONE`, `SCUD_ADMIN_PASSWORD`,
`SCUD_ACCESS_TOKEN`.

## Генерация событий безопасности (`--events`)

Флаг `--events` у `scud relay run` включает сценарий, который циклически меняет
поля `stat` устройства, заставляя backend эмитить *device-asserted* события
безопасности (единственные, доступные релейному тракту — интрузия/tamper/пороги
по протоколу платы не передаются). Один цикл даёт три события:

| Мутация stat | Событие безопасности backend |
|---|---|
| `power` MAINS→BATTERY | «Питание переключено на резервную батарею (пропало основное питание)» |
| `status` = ERROR | «Устройство сообщило об ошибке (статус ERROR)» |
| `status` = OFFLINE | offline (heartbeat_timeout) |

Каждое событие edge-triggered на стороне backend, поэтому сценарий чередует
инцидент с шагом-восстановлением baseline — грань перезаряжается и события идут
на каждом цикле.

```bash
# 20 плат, каждая шлёт инцидент раз в ~15 c (±15% джиттер)
scud relay run --count 20 --base-id 5001 --events --events-interval 15
```

## TLS-подключение (`--tls`)

Протокол (§4) допускает два способа подключения платы: порт 1883 без TLS и порт
8883 с TLS 1.2. Брокер сам собран без TLS, поэтому 8883 терминируется отдельным
сервисом `mqtt_tls`, который проксирует расшифрованный поток в `mqtt:1883`.

Сертификат брокера подписан приватным CA, поэтому клиенту нужен `--cafile`:

```bash
scud relay run --count 1 --base-id 1001     --host scud-dev.5.35.126.197.sslip.io --tls --cafile /certs/ca.crt
```

Имя хоста проверяется по SAN сертификата, поэтому адресовать брокер нужно тем же
именем, что попало в сертификат. Для быстрой пробы доступности порта без
проверки цепочки есть `--tls-insecure`.

## Тесты

```bash
# Локально
pip install -e ".[all]"
pytest tests/

# В Docker
docker compose --profile tests run --rm emulator_tests
```

Юнит-тесты сверяют кодировку/декодировку протокола с golden-векторами и
проверяют поведение устройства (`test_relay_protocol_golden.py`,
`test_relay_crosscheck_backend.py`, `test_relay_device_behavior.py`,
`test_relay_rampup_jitter.py`, `test_relay_resilience.py`).

## Docker

```bash
cd emulator

# Юнит-тесты
docker compose --profile tests run --rm emulator_tests

# Флот relay-устройств из образа (нужен доступный брокер)
docker build -t scud-relay .
docker run --rm scud-relay --count 10 --host <broker-host> --port 1883 --base-id 5001
```

System-e2e (`tests/system`) НЕ поднимает этот compose-стек: relay-двойник
монтируется в `system_tests` из `src/scud_relay` напрямую и запускается
in-process через `RelayRunner`.

## Параметры `scud relay run`

| Параметр | По умолчанию | Описание |
|---|---|---|
| `--count` / `-n` | `1` | Количество relay-устройств во флоте |
| `--host` | `localhost` | Хост MQTT-брокера |
| `--port` | `1883`, либо `8883` при `--tls` | Порт MQTT-брокера |
| `--base-id` | `1` | id первого устройства (`relay-{id}`); дальше инкремент |
| `--version` | `2` | Версия прошивки (2 поддерживает LOCK_OPEN/LOCK_CLOSE) |
| `--ramp-up` | `0.0` | Растянуть подключения на N секунд (сглаживает connect-шторм) |
| `--events` / `--no-events` | `--no-events` | Циклически генерировать события безопасности (power-loss/ERROR/offline) |
| `--events-interval` | `30.0` | Секунд между шагами сценария событий (с `--events`) |
| `--photos` / `--no-photos` | `--no-photos` | К каждому инциденту с событием прикладывать серию фото в `photos` (включает `--events`) |
| `--photos-per-incident` | `5` | Кадров в серии (1..16); каждый кадр — отдельный JPEG с уникальным COM-сегментом и нулевым хвостом |
| `--tls` / `--no-tls` | `--no-tls` | Подключаться по TLS 1.2 (порт 8883 протокола §4) |
| `--cafile` | — | CA-сертификат, которым подписан сертификат брокера (env `SCUD_MQTT_CAFILE`) |
| `--tls-insecure` | выкл. | Не проверять сертификат — только проба доступности |
