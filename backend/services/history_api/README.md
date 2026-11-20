# History API - Подсистема хранения исторических данных

Микросервис для хранения и анал итики исторических данных системы СКУД.

## Описание

History API обеспечивает:
- Хранение телеметрии датчиков (минимум 2 года)
- Журналирование событий безопасности
- История выполненных команд
- Хранение медиа-файлов (фото и видео) при срабатывании датчиков взлома
- Аналитика и агрегированные данные

## Технологический стек

- Python 3.11+
- Tornado (асинхронный веб-фреймворк)
- PostgreSQL 15 с партиционированием
- psycopg2 (с асинхронной оберткой shared.database)
- Docker

## Масштаб

- ~5000 шкафов
- ~167 записей телеметрии/секунду
- ~5.26 млрд записей за 2 года
- Партиционирование по месяцам для оптимизации

## Архитектура

### Таблицы БД:
- `sensor_telemetry` (партиционирована) - телеметрия датчиков
- `security_events` - события безопасности
- `command_history` - история команд
- `locker_media` - медиа-файлы (фото и видео) шкафов (BYTEA inline-хранение)
- `telemetry_aggregations` - предрасчитанные агрегаты

### Фоновые сервисы:
- `AggregationService` (`app/services/aggregation.py`) - периодически рассчитывает часовые, дневные и месячные агрегаты из `sensor_telemetry` в `telemetry_aggregations` (upsert). Использует один SQL-запрос `INSERT...SELECT...ON CONFLICT DO UPDATE` на каждый тип агрегации.
- `RetentionService` (`app/services/retention.py`) - удаляет данные старше заданного срока хранения: дропает старые партиции `sensor_telemetry`, чистит строки из `security_events`, `command_history`, `locker_media`, `telemetry_aggregations`, создаёт партиции на 3 месяца вперёд.

## Запуск

```bash
# Через Docker Compose
cd backend
docker compose up history_api

# Локально
cd backend/services/history_api
python -m app.main
```

## API

### Public endpoints (через Apache gateway, требуют `X-Remote-User`)

- `POST /api/v1/telemetry` — запись телеметрии (batch, до 1000 записей)
- `GET /api/v1/telemetry` — получение телеметрии с фильтрами
- `POST /api/v1/events/security` — запись события безопасности
- `GET /api/v1/events/security` — получение событий
- `POST /api/v1/commands/history` — **идемпотентная** запись/обновление истории команды (по `command_id`)
- `GET /api/v1/commands/history` — получение истории
- `GET /api/v1/media` — список медиа-файлов с фильтрами (роль OPERATOR+)
- `GET /api/v1/media/{id}` — скачать медиа-файл (отдаёт байты с `Content-Type`; в `Content-Disposition` имя файла с корректным расширением по `mime_type`, например `media-42.jpg`)
- `GET /api/v1/events/security/{id}/media` — все медиа к событию безопасности
- `GET /api/v1/analytics/sensors` — агрегированные данные

### Internal endpoints (только из Docker-сети, без `X-Remote-User`)

Закрыты снаружи через Apache (`<Location /internal> Require all denied`). Используются продьюсерами locker_api для записи без user-context.

**HMAC-подпись (SEC-05).** Каналы записи `events/security`, `telemetry`, `media`
подписываются HMAC-ключом `INTERNAL_HISTORY_HMAC_KEY` (заголовки
`X-Internal-Timestamp` + `X-Internal-Signature` над `sha256(body)`, окно ±30 с) —
тот же generic-формат, что у `notification_api /internal/api/v1/otp/send`.
Невалидная/устаревшая подпись → `401`. Ключ ОБЯЗАН совпадать в `history_api`,
`locker_api` и `auth_api`. Если не сконфигурирован (dev/CI) — проверка
пропускается (fail-open; перевод в fail-closed — follow-up для всех HMAC-эндпойнтов).

**Идемпотентность (ARC-02).** При at-least-once-доставке (HTTP-ретраи +
re-delivery из outbox) каждый payload несёт продьюсер-генерируемый `event_uid`
(UUID, стабильный между ретраями — живёт в outbox-строке). Дубликат по `event_uid`
не создаёт новую строку (`ON CONFLICT DO NOTHING`); `events/security` и `media`
отвечают `200 {"status":"duplicate"}`. Для партиционированной `sensor_telemetry`
UNIQUE-индекс — `(event_uid, recorded_at)` (Postgres требует включать ключ
партиционирования). `command_history` остаётся идемпотентным по `command_id`;
статус продвигается только вперёд по рангу, а смена одного терминального статуса
на другой (поздний `executed` после `failed` по TTL и наоборот) не меняет ни
статус, ни `executed_at`/`failed_at`, `result` и `error_message`.

- `POST /internal/api/v1/events/security`
- `POST /internal/api/v1/telemetry`
- `POST /internal/api/v1/commands/history`
- `GET /internal/api/v1/commands/history` — обобщённая выборка истории команд
  с фильтрами `command_type`, `status`, диапазоном по `executed_at`
  (`executed_from`/`executed_to`, ISO-8601) и `order`
  (`created_at_desc` по умолчанию / `executed_at_asc`); `limit`
  (default `1000`, max `5000`) / `offset`. Ответ — `PaginatedResponse`
  (`items`, `total`, `limit`, `offset`) с ПОЛНЫМИ строками `command_history`
  (тот же формат, что и публичный `GET /api/v1/commands/history`). Используется
  `reports_api` для отчётов (например, `locker_open`:
  `command_type=open&status=executed&order=executed_at_asc`).
- `POST /internal/api/v1/media` — загрузка медиа-файла от locker_api
  (multipart/form-data; поля `locker_id`, `kind` (`photo`), `mime_type`,
  `captured_at`, `file`, опционально `security_event_id`,
  `event_sensor_id`+`event_occurred_at` для ±30 с window-match, `event_uid`)

### Producer → Channel → Event_type

Кто и какие данные пишет в history_api:

| Источник (locker_api) | Канал | event_type / таблица | Severity |
|---|---|---|---|
| `state_monitor` (numeric/boolean threshold) | `/internal/api/v1/events/security` | `threshold_violation` | high |
| `state_monitor` (sensor_type_code='intrusion') | `/internal/api/v1/events/security` | `intrusion` | high |
| `state_monitor` (sensor_type_code='tamper') | `/internal/api/v1/events/security` | `door_tamper` | critical |
| MQTT ingest (offline-детект, через `OfflineEventProducer`) | `/internal/api/v1/events/security` | `offline` | medium |
| MQTT `rep` по уже терминальной команде (через `CommandLateExecutionProducer`) | `/internal/api/v1/events/security` | `command_late_execution` | high |
| MQTT ingest (`stat`, через `TelemetryProducer`) | `/internal/api/v1/telemetry` | таблица `sensor_telemetry` | — |
| `LockerCommandHandler` + `CommandResultApplier` (через `CommandHistoryProducer`) | `/internal/api/v1/commands/history` | таблица `command_history` | — |
| MQTT ingest (`photos`, через `MediaProducer`) | `/internal/api/v1/media` | таблица `locker_media` | — |

Доставка из locker_api идёт через persistent outbound queue (таблица `outbound_history_events` в `scud_lockers`) — недоступность history_api не приводит к потере событий.

## Медиа (фото)

При срабатывании датчиков `intrusion`/`tamper` шкаф может приложить медиа-файлы. Они хранятся
inline в таблице `locker_media` (поле `bytes BYTEA`).

### Структура таблицы `locker_media`

| Поле | Тип | Описание |
|---|---|---|
| `id` | UUID | Первичный ключ |
| `locker_id` | UUID FK | Шкаф-источник |
| `kind` | ENUM(`photo`) | Тип файла |
| `bytes` | BYTEA | Содержимое файла (inline) |
| `content_type` | TEXT | MIME-тип (`image/jpeg`, `image/png`) |
| `size_bytes` | INTEGER | Размер в байтах (до 10 МБ) |
| `duration_ms` | INTEGER NULL | Длительность; для photo всегда null |
| `security_event_id` | UUID NULL FK | Привязка к событию безопасности |
| `security_event_uid` | UUID NULL | `event_uid` события для отложенной привязки (relay-фото) |
| `metadata` | JSONB | Для relay-кадра: `relay_event_id`, `photo_id`, `photos_cnt`, `series_uid` |
| `created_at` | TIMESTAMPTZ | Время записи |

### Привязка к событию безопасности

По приоритету:

1. `security_event_id` передан явно — медиа привязывается к нему напрямую.
2. Передан `security_event_uid` — ищется событие с этим `event_uid`. Если его
   ещё нет (кадр relay-серии обогнал intrusion-событие в outbox), медиа
   пишется без привязки и привязывается позже: сразу после вставки
   (`relink_media`) и при записи события (`link_pending_to_event` в
   `POST /internal/api/v1/events/security`). Окно в этом случае не используется.
3. Legacy: переданы `event_sensor_id` + `event_occurred_at` —
   `MediaRepository.find_event_in_window` ищет ближайшее событие того же
   `locker_id`/`sensor_id` в окне ±30 секунд. События relay-серий
   (`metadata.series_uid`) окном не матчатся.

Ошибка привязки по uid → 500; outbox повторит доставку, и путь дубликата
повторит привязку.

Relay-фото хранится по одному JPEG на строку. `GET /api/v1/events/security/{id}/media`
отдаёт кадры по `metadata.photo_id` по возрастанию, затем по `captured_at`/`id`;
legacy-записи без `photo_id` идут последними.

### REST-эндпойнты (роль OPERATOR+)

- `GET /api/v1/media` — список медиа с фильтрами `locker_id`, `kind`, `security_event_id`,
  диапазоном `created_from`/`created_to`; `limit`/`offset`
- `GET /api/v1/media/{id}` — скачать файл; ответ — бинарный с `Content-Type` из записи; заголовок `Content-Disposition: inline` содержит имя файла с корректным расширением, выведенным из `mime_type` (например `media-42.jpg`)
- `GET /api/v1/events/security/{id}/media` — все медиа, привязанные к событию безопасности

## Переменные окружения

| Переменная | По умолчанию | Описание |
|---|---|---|
| `DB_HOST` | `localhost` | Хост PostgreSQL |
| `DB_PORT` | `5432` | Порт PostgreSQL |
| `DB_USER` | `admin` | Пользователь БД |
| `DB_PASSWORD` | `password` | Пароль БД |
| `DB_NAME` | `scud_history` | Имя базы данных |
| `APP_PORT` | `8002` | Порт HTTP-сервера |
| `AGGREGATION_INTERVAL_SECONDS` | `300` | Интервал агрегации (сек) |
| `RETENTION_INTERVAL_SECONDS` | `3600` | Интервал проверки ретеншена (сек) |
| `RETENTION_MONTHS` | `24` | Срок хранения данных (мес) |

## Тестирование

Все тесты запускаются исключительно в Docker-контейнере:

```bash
cd backend
docker compose --profile tests up --build history_api_tests
```

Образ `history_api_tests` собирает unit- и integration-тесты вместе. Подробнее — в `tests/README.md`.

## Документация

OpenAPI спецификация: [`openapi/openapi.yaml`](openapi/openapi.yaml).

<!-- TODO: создать DEVELOPER_GUIDE.md по образцу locker_api / user_api -->

