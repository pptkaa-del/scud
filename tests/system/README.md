# Системные интеграционные тесты СКУД

Полный E2E-тест всей системы: шлюз → backend API → релейный двойник устройства →
уведомления.

## Архитектура

```
Тест → Apache Gateway (cookie auth) → [user_api, locker_api, notification_api]
Тест → RelayRunner (in-process device double) → MQTT broker → locker_api
```

Устройство поднимается не отдельным контейнером, а прямо в процессе теста:
`utils/relay_runner.py` запускает `scud_relay` (тот же код, что использует
эмулятор) в фоновом asyncio-потоке и подключает его к брокеру MQTT как
конкретный `relay_id`. Тест получает объект `RelayRunner`, работает с ним как
с живым устройством (открывает/закрывает дверь, шлёт телеметрию) и глушит его
по выходу из `with`.

## Запуск

### 1. Запустить backend

```bash
cd backend
docker compose --env-file .env.fast up -d --build --wait
```

### 2. Запустить системные тесты

```bash
cd tests/system

# Все сценарии:
docker compose --env-file .env.fast run --rm --build system_tests

# Конкретный сценарий:
docker compose --env-file .env.fast run --rm --build system_tests pytest -vv scenarios/test_relay_mqtt_e2e.py

# С фильтром по имени:
docker compose --env-file .env.fast run --rm --build system_tests pytest -vv -k "open_close"
```

Флаг `--build` обязателен: образ `system_tests` запекает содержимое `tests/system`
на этапе сборки.

Быстрый путь для локальной разработки — `./run-fast.sh` поднимает backend и
запускает тесты одной командой.

> **Важно:** Тесты подключаются к сети `backend_net`, которую создаёт backend docker-compose.
> Backend должен быть запущен до старта тестов.

## Структура

```
tests/system/
├── docker-compose.yaml        # Только test runner (backend — отдельно)
├── Dockerfile                 # python:3.13-slim
├── requirements.txt
├── pytest.ini
├── run-fast.sh                 # backend + тесты одной командой (.env.fast)
├── conftest.py                 # Глобальные фикстуры
├── utils/
│   ├── api_client.py           # HTTP-клиент для gateway и backend API
│   ├── mqtt_locker.py          # Создание mqtt-шкафа через gateway API + relay_id
│   ├── relay_runner.py         # RelayRunner — фоновый scud_relay double
│   ├── service_logs.py         # Чтение docker-логов backend-сервисов
│   ├── timing.py               # Хелперы синхронизации со state monitor
│   └── wait.py                 # wait_for() — polling helper
└── scenarios/
    ├── test_alarm_thresholds.py
    ├── test_command_2fa.py
    ├── test_open_close_notifications.py
    ├── test_relay_mqtt_e2e.py
    ├── test_reports.py
    ├── test_sensor_value_types.py
    ├── test_state_transitions.py
    └── test_tamper_media.py
```

## Дизайн тестов

Каждый тест:
1. Создаёт **свои** ресурсы (пользователей, шкафы, датчики)
2. Проводит сценарий
3. Удаляет все созданные ресурсы, даже при падении

Провизионирование шкафа и подключение устройства выглядят так:
```python
relay_id = _random_relay_id()
locker = _create_mqtt_locker(gateway, relay_id)
with RelayRunner(relay_id) as runner:
    wait_for(lambda: gateway.get_connectivity(locker["id"]) == "online")
    gateway.send_command(locker["id"], "open")
    wait_for(lambda: runner.device.door == Door.OPEN)
    ...
# Автоматическая остановка устройства при выходе
```

## Добавление новых сценариев

1. Создать файл `scenarios/test_my_scenario.py`
2. Использовать фикстуры из `conftest.py`, провизионирование шкафа через
   `utils/mqtt_locker.py` и устройство через `utils/relay_runner.RelayRunner`
3. Документировать ожидаемое поведение в docstring теста

## Переменные окружения

| Переменная                     | Описание                            | Default                  |
|---------------------------------|--------------------------------------|--------------------------|
| GATEWAY_URL                     | URL Apache Gateway                   | http://apache_gateway    |
| ADMIN_PHONE                     | Телефон администратора               | +79000000000             |
| ADMIN_PASSWORD                  | Пароль администратора                | changeme                 |
| TEST_TIMEOUT                    | Таймаут ожидания событий (сек)       | 180                      |
| MQTT_BROKER_HOST                | Хост MQTT-брокера для RelayRunner    | host.docker.internal     |
| MQTT_BROKER_PORT                | Порт MQTT-брокера                    | 1883                     |
| STATE_MONITOR_CHECK_INTERVAL_S  | Интервал state monitor (сек)         | 60                       |
| DEFAULT_TELEMETRY_INTERVAL_S    | Интервал телеметрии по умолчанию (сек) | 30                      |
| SAVE_ARTIFACTS                  | Сохранять артефакты теста в /artifacts | (выкл)                  |
