# Locker API - Подсистема контроля и управления шкафами

API для управления телекоммуникационными шкафами в системе ScudDocs СКУД.

## Обзор

Locker API предоставляет два интерфейса:

1. **Control Gateway** - REST API для управления шкафами, датчиками, командами и пороговыми значениями
2. **Device Gateway** - MQTT-транспорт для подключения физических устройств (релейных плат) через брокер Mosquitto

## Архитектура

### Основные компоненты

- **Шлюз управления шкафами (Control Gateway)** - REST API для внутренних компонентов системы
- **Шлюз шкафа (Device Gateway)** - MQTT-транспорт для релейных плат (брокер Mosquitto, аутентификация по broker-пользователю `relay-N`)
- **Модуль хранения данных** - БД с информацией о шкафах, датчиках, командах и пороговых значениях
- **Модуль управления шкафами** - Command Queue для доставки команд на устройства
- **Модуль контроля состояния** - State Monitor для проверки пороговых значений датчиков

### Структура БД

- `lockers` - Шкафы (UUID, инвентарный номер, местоположение, `public_key` — legacy-поле от удалённой Ed25519-аутентификации устройств по WebSocket; для MQTT-реле не используется)
- `sensors` - Датчики (UUID, serial_number, sensor_type_id FK, current_value)
- `sensor_types` - Справочник типов датчиков (id, code, name, unit, value_type)
- `sensor_thresholds` - Пороговые значения для датчиков
- `locker_commands` - Журнал команд (open, close, reboot, config, relay_on, relay_off, relay_restart)
- `locker_relays` - Каналы блока реле платы (r1..r4): настройки и последнее состояние из stat
- `locker_state_history` - История изменения состояний

**Примечание:** `sensor_types` является таблицей-справочником (не ENUM), что позволяет гибко добавлять новые типы датчиков через API.

#### Состояния перечислимых датчиков (`sensor_types.states`)

`current_value` всегда хранит сырое значение протокола. Подписи живут в
`sensor_types.states` (JSONB, NULL — тип не перечислимый): массив
`{value, label, severity}`, `severity` — `normal`/`warning`/`alert` (только для
подсветки в UI). `value` — int для numeric и bool для boolean; для `string`
состояния запрещены; не более 32 элементов, `label` 1..100 символов, значения
уникальны. Датчик отдаёт `display: {label, severity}`, вычисляемый при каждом
чтении из `states` его типа; без `states` — `display: null`. Значение вне
`states` → «Неизвестное значение (N)» (warning), null → «Нет данных» (эти
служебные подписи заданы в коде).

Дефолты (`power`, `net_type`, `error`, `door`, `intrusion`) сеются блоком
`sensor-type-states:begin/end` в `001_init.sql` (идемпотентно, `states IS NULL`,
правки администратора не затираются; типы `net_type`/`error` создаются
`ON CONFLICT DO NOTHING`). Выкатка: блок выполняется до пересборки
`locker_api`, см. `backend/DEPLOYMENT.md` §6.

Управление — `/api/v1/sensor-types`: `POST` принимает необязательный `states`;
в `PUT` ключ отсутствует — без изменений, `null` — очистка, массив — полная
замена (при смене `value_type` без `states` текущие перепроверяются).
`400` — невалидные `states`; `409` — переход NULL → список при активных
min/max-порогах на активных датчиках типа, либо удаление значения, на которое
указывает активный `fixed_value`. `DELETE` → `409` только при датчиках этого
типа; все типы, включая `power`/`net_type`/`error`, редактируются свободно.

Для датчиков с `states` пороги `min_value`/`max_value` отклоняются (`400`), а
числовой `fixed_value` должен входить в `states`.

Новый перечислимый датчик: задать `states` типу через API; если значения
присылает плата — строка `_append_reading` в `app/mqtt/ingest.py` и член
`SensorCode`.

## API Endpoints

### Control Gateway (REST)

#### Шкафы
- `GET /api/v1/lockers` - Список шкафов с фильтрами (`connectivity`, `door`, `operational_status`, `limit`, `offset`).
  **Ограничен Допуском:** возвращаются только шкафы, видимые вызывающему. `worker` видит
  ровно шкафы с активным Допуском; `operator`, `administrator` и `worker` с полным доступом —
  все шкафы. Область считается из заголовка `X-Remote-Lockers` (ACM) через
  `shared.access_control.resolve_locker_scope` и попадает в SQL-фильтр `id = ANY(...)`
  **и в `get_all`, и в `count`** — поэтому `total` соответствует `items`. Нет Допусков —
  не `403`, а `200` с пустым списком и `total: 0`.
  Поддерживает опциональный параметр `?view=location` — сокращённый ответ только с координатами (`id`, `lat`, `lon`, `address`). Без `view` возвращается полный объект `Locker`. Допустимые значения для списка определены в `ALLOWED_LIST_VIEWS` (`app/handlers/locker.py`).
  Опциональный `?q=<подстрока>` — case-insensitive подстрочный поиск по `inventory_number` ИЛИ `location_address` (один параметр, оба поля; комбинируется через `AND` с остальными фильтрами). Значение обрезается по краям; пустое/whitespace игнорируется. LIKE-метасимволы `%`, `_` и `\` в значении экранируются и ищутся как литералы. Максимальная длина после trim — 100 символов, превышение → `400`.
- `POST /api/v1/lockers` - Создать шкаф
- `POST /api/v1/lockers/batch` - Массовое получение шкафов по списку UUID. Тело: `{"locker_ids": [...], "view": "brief"|"location" (опц.)}`. Лимит — 100 id за запрос. Ответ — словарь `id → Locker | LockerBrief | LockerLocation | null`. Без `view` возвращается полный `Locker`. Та же область видимости, что и у списка: запрошенные id пересекаются с Допусками, шкаф вне области не вытащить в обход `GET /lockers` (на уровне ACM эндпоинт и так `operator+`, пересечение — второй рубеж).
- `GET /api/v1/locker/{id}` - Получить шкаф
- `PUT /api/v1/locker/{id}` - Обновить шкаф
- `DELETE /api/v1/locker/{id}` - Удалить шкаф (soft delete)
- `GET /api/v1/locker/{id}/state` - Получить состояние
- `PUT /api/v1/locker/{id}/state` - Обновить состояние

#### Типы датчиков
- `GET /api/v1/sensor-types` - Список типов датчиков
- `POST /api/v1/sensor-types` - Создать тип датчика
- `GET /api/v1/sensor-types/{id}` - Получить тип датчика
- `PUT /api/v1/sensor-types/{id}` - Обновить тип датчика
- `DELETE /api/v1/sensor-types/{id}` - Удалить тип датчика

#### Датчики
- `GET /api/v1/locker/{id}/sensors` - Список датчиков шкафа (с полем `display`, см. выше)
- `POST /api/v1/locker/{id}/sensors` - Создать датчик (требуется sensor_type_id)
- `GET /api/v1/locker/{id}/sensors/{sensor_id}` - Получить датчик
- `PATCH /api/v1/locker/{id}/sensors/{sensor_id}` - Обновить статус датчика
- `DELETE /api/v1/locker/{id}/sensors/{sensor_id}` - Удалить датчик

#### Пороговые значения
- `GET /api/v1/locker/{id}/sensors/{sensor_id}/thresholds` - Список порогов
- `POST /api/v1/locker/{id}/sensors/{sensor_id}/thresholds` - Создать порог
- `GET /api/v1/locker/{id}/sensors/{sensor_id}/thresholds/{threshold_id}` - Получить порог
- `PUT /api/v1/locker/{id}/sensors/{sensor_id}/thresholds/{threshold_id}` - Обновить порог
- `DELETE /api/v1/locker/{id}/sensors/{sensor_id}/thresholds/{threshold_id}` - Удалить порог

#### Блок реле
- `GET /api/v1/locker/{id}/relays` - Каналы r1..r4: фактическое `reported_state` (0 выкл, 1 вкл, 2 ошибка, null — нет данных), настройки, `allowed_actions` для текущего пользователя и `pending_command`; плюс `has_relays`, `board_version`, `connectivity`, `reported_at` (= `last_seen_at`). worker+ с Допуском
- `PATCH /api/v1/locker/{id}/relays/{1..4}` - Настройки канала (`name`, `purpose`, `control_enabled`, `is_protected`, `is_active`), только administrator; ошибки тела — 422

Каналами управляют командами `relay_on` / `relay_off` / `relay_restart` с `parameters: {"relay": N}` через `POST /api/v1/locker/{id}/commands` (operator+; защищённый канал — только administrator). На канал допускается одна незавершённая команда (частичный уникальный индекс `ux_locker_commands_relay_active`).

`has_relays`/`board_version` и строки `locker_relays` пишет MQTT-ингест по stat: v1 — блок есть всегда, v2 — при `relay_en = 1`; при `relay_en = 0` состояния каналов не пишутся. Строки каналов создаются при первом stat с блоком реле, в БД пишутся только изменения. Новые каналы пока создаются с `control_enabled = true`.

#### Команды
- `GET /api/v1/locker/{id}/commands` - История команд
- `POST /api/v1/locker/{id}/commands` - Создать команду (см. «2FA-подтверждение команд» ниже — для пользователей с `require_2fa=true` ответ может быть `202` вместо `201`)
- `POST /api/v1/locker/{id}/commands/confirm` - Подтвердить step-up OTP и поставить команду в очередь
- `GET /api/v1/locker/{id}/commands/{command_id}` - Получить команду

### MQTT relay transport (релейные платы)

locker_api подключает релейные платы по **MQTT** (брокер
Mosquitto, plaintext `:1883` внутри `backend_net`; наружу — `:1883` без TLS
либо `:8883` через терминатор `mqtt_tls`). Шкаф с `transport='mqtt'` идентифицируется целым
`relay_id`; устройство публикует бинарные `stat`/`rep` (в общий топик либо в
`stat/{relay_id}`), сервер шлёт `com/{relay_id}`.

- Создание: `POST /api/v1/lockers` с `{"transport":"mqtt","relay_id":N}` — провижионит
  broker-пользователя `relay-N` (логин/пароль на shared-томе брокера, watcher делает SIGHUP).
- Кодек/ингест: `app/mqtt/` (`relay_codec`, `transport`, `ingest`, `results`, `offline_monitor`,
  `provisioning`); клиент — `shared.mqtt_client` (обёртка над завендоренным gmqtt).
- Команды коррелируются по `locker_commands.relay_cmd_id` (uint32), эхо в `rep`.
- Offline-детект: нет `stat` > `MQTT_OFFLINE_TIMEOUT_S` (350с) или `stat=0`.
- Топик `photos` (фото несанкц. открытия) — каждый кадр — отдельный JPEG и сразу уходит
  в `media`; первый кадр серии порождает `intrusion` security event (см. «Relay-фото»
  ниже).
- Несанкционированное открытие (протокол платы §7.1) распознаёт сервер: значение датчика
  `door` (его пишет только `stat`) сменилось с закрыто на открыто, а последняя
  опубликованная в `com/{id}` до этого `stat` команда замка — не OPEN (или её нет) →
  `intrusion` security event и датчик `intrusion` = true; закрытие двери сбрасывает датчик
  в false. Шкаф в `normal` переходит в `alarm` и получает постоянное уведомление операторам
  (`event=intrusion`); в `maintenance` и уже в `alarm` пишется только событие. Команды, отвергнутые платой (`rep` FAILURE / NOT_SUPPORTED, в том числе после
  истечения TTL), не считаются. Плата без камеры сама о взломе не сообщает; плата с камерой
  даёт второе `intrusion` — по серии фото. Правило опирается на retained `com/{id}`, а
  брокер работает с `persistence false`: после его рестарта retained-команд нет, и
  перезагрузившаяся плата остаётся без действующей команды, хотя сервер считает
  действующей последнюю опубликованную.
- Лимит тела `photos`: `MAX_RELAY_PHOTO_BYTES` (env `MAX_MEDIA_TRANSFER_BYTES`, default 10 МиБ) —
  кодек отвергает кадр с бо́льшим объявленным размером как malformed (SEC-04, guard от
  decode-bomb/memory-pressure); брокер дополнительно режет PUBLISH по `message_size_limit`.

**Trust boundary.** Устройство публикует либо в **общие** `stat`/`rep`/`photos`, либо в
per-id `stat/{id}`/`rep/{id}`/`photos/{id}` (ПИВ v2); backend подписан на обе схемы.
В per-id топике суффикс сверяется с `device_id` внутри кадра, и расхождение отбрасывается,
поэтому подделка чужой телеметрии там невозможна. В **общем** топике такой привязки нет:
любой валидный broker-пользователь может опубликовать кадр с чужим `relay_id`. Права на
общие топики выдаются ради совместимости с прошивками прежней ревизии; когда схема парка
подтвердится, три строки в `provisioning.acl_block` снимаются и дыра закрывается.
Forged-запись в общий топик задокументирована и в `ingest.py` (stale/forged `stat=0`
порвёт маршрут лишь до следующего реального `stat`). TLS — порт 8883, sidecar `mqtt_tls`.

Подробности протокола (топики, бинарные форматы `stat`/`rep`/`com`/`photos`,
поток команд, provisioning и ACL): [`openapi/openapi_relay.md`](openapi/openapi_relay.md).

### Device Gateway (MQTT relay)

Транспорт устройств — MQTT (брокер Mosquitto), см. секцию «MQTT relay transport
(релейные платы)» выше. WebSocket-шлюз устройств удалён вместе с
Ed25519-аутентификацией по challenge-response, heartbeat-сессиями и
JSON-протоколом `auth`/`telemetry`/`command`/`ping` — их заменил бинарный
MQTT-протокол `stat`/`rep`/`com`/`photos`, описанный в
[`openapi/openapi_relay.md`](openapi/openapi_relay.md). Значение
`LockerTransport.ws` в БД сохранено только для чтения исторических записей и
не используется для живых подключений.

## Аутентификация и Trust Boundary

### Control Gateway (REST API для управления)

**Аутентификация через user-context:**
- Заголовок `X-Remote-User` с ID пользователя (integer)
- **ВАЖНО:** Locker API **доверяет** прокинутому user-context и **НЕ выполняет** валидацию пользователя
- Аутентификация и авторизация выполняется на уровне API Gateway (вне Locker API)
- User ID используется только для логирования действий (`created_by`, `updated_by`)

**Контракт:**
- Gateway гарантирует, что `X-Remote-User` содержит ID аутентифицированного пользователя
- Gateway проверяет права доступа пользователя к операциям перед проксированием запроса
- Locker API использует ID из заголовка "as is" без дополнительных проверок

### Device Gateway (MQTT relay для устройств)

**Аутентификация устройств — broker-пользователь `relay-N`:**
- Релейная плата аутентифицируется у Mosquitto собственными логином/паролем
  (`relay-N`), выданными при provisioning (`POST /api/v1/lockers` с
  `transport=mqtt`); подробности и trust boundary Фазы 1 — см. секцию
  «MQTT relay transport (релейные платы)» выше.
- Ed25519 challenge-response и REST-provisioning публичного ключа удалены
  вместе с WebSocket-шлюзом устройств; `lockers.public_key` остаётся только
  как legacy-поле для уже отключённых WS-устройств.
- После аутентификации у брокера плата публикует `stat`/`rep`/`photos` и
  подписана на `com/{relay_id}` — телеметрия и команды идут этим каналом.

## Фоновые сервисы

### Command Queue Processor
- Интервал: 5 секунд (env `COMMAND_QUEUE_PROCESSOR_INTERVAL_S`, default 5)
- Функция: Обработка очереди команд и отправка на онлайн устройства
- Каждый тик сначала выметает команды старше `COMMAND_TTL_S` (default 60 с от
  `created_at`): они атомарно становятся `failed` с причиной «истёк срок ожидания
  ответа шкафа» вместе с outbox-событием `command_history`; та же очистка батчами
  выполняется при старте сервиса. Просроченные команды не выбираются и не
  заявляются — после TTL команда на плату не уходит.
- `sent` публикуется в `command_history` только после фактической отправки;
  неудачная отправка возвращает claim в `pending` для повтора в пределах TTL.
- Терминальные `executed`/`failed` не перезаписываются поздним или повторным `rep`
  (`outcome=stale_rep`). `SUCCESS` по уже терминальной команде порождает
  `command_late_execution` (severity `high`) и одно постоянное уведомление
  операторам/администраторам (`event=command_late_execution`). Исключение —
  повтор `SUCCESS` по выполненному `close`: retained `CLOSE` штатно
  переисполняется платой при переподписке, это тихий `stale_rep`.
- Пока сессия с MQTT-брокером оборвана, `MqttTransport.send_command` не
  публикует команду и возвращает `False` (claim возвращается в `pending`).
- Prometheus: `scud_command_lifecycle_total{transition="ttl_expired"}`,
  `scud_mqtt_relay_messages_total{outcome="stale_rep"}`,
  `scud_security_events_total{type="command_late_execution",severity="high"}`.

### State Monitor
- Интервал: 60 секунд
- Функция: Проверка пороговых значений датчиков и отправка алертов
- При переводе шкафа в `alarm` пишет структурированную строку лога уровня `WARNING`
  (или `CRITICAL` для `sensor_type_code='tamper'`) с полями `event=security_alarm`,
  `type`, `severity`, `locker_id`, `sensor_id` — пригодна для SIEM-агрегации.
- Prometheus-метрика: `scud_security_events_total{service,type,severity}` — инкрементируется
  в moment постановки security_event в outbound-очередь (продьюсеры
  `ThresholdViolationProducer`, `IntrusionProducer`, `DoorTamperProducer`,
  `OfflineEventProducer`, `CommandLateExecutionProducer`). Значения label `type` соответствуют `SecurityEventType`
  из `shared.enums`; `severity` — `EventSeverity`.

## Устойчивость к штормам реле / масштабирование

### Тёплый пул соединений

`AsyncPostgres` по умолчанию открывает `minconn = max_workers` соединений
при старте (`DB_MIN_CONNECTIONS_LOCKER`, default 20). `psycopg2.ThreadedConnectionPool`
создаёт соединения под глобальным lock'ом пула; при холодном пуле (`minconn << maxconn`)
каждый `getconn` в момент всплеска нагрузки сериализуется за TCP-handshake (lock convoy).
Тёплый пул исключает новые соединения на hot-path: `getconn` всегда берёт уже открытое.
Практически: потолок стабильного ingress поднялся с ~200 до ~550 реле при той же БД.

### Ingest bulkhead (ограничение параллелизма MQTT-ингеста)

gmqtt диспатчит каждое входящее сообщение как `asyncio.ensure_future` без ограничений.
При шторме подключений/stat тысячи задач одновременно борются за 20 соединений пула,
пиная `/health` и доставку команд в голодание.

`IngestBulkhead` (`app/mqtt/bulkhead.py`) вводит два порога:

- `MQTT_INGEST_MAX_CONCURRENCY` (default 10) — семафор над `handle_message`;
  остальные 10 слотов пула остаются для `/health`, команд и `OutboundDispatcher`.
- `MQTT_INGEST_MAX_PENDING` (default 1000) — суммарный лимит (выполняющихся +
  ожидающих семафора). Loss-tolerant сообщения сверх лимита **отбрасываются** без
  обработки, не теряясь в очереди незаметно: каждый сброс инкрементирует счётчик
  `scud_mqtt_relay_messages_total{outcome="dropped_overload"}`. Сообщения `rep`
  (ответы реле на команды сервера) **никогда не отбрасываются** — они exempt.

Кадры `photos` идут отдельной полосой `ByteBudgetBulkhead` и не занимают слоты
stat/rep:

- `MQTT_INGEST_PHOTO_MAX_CONCURRENCY` (default 2) — свой семафор. Сумма с
  `MQTT_INGEST_MAX_CONCURRENCY` должна быть ≤ `DB_MAX_WORKERS_LOCKER` − 4.
- `MQTT_INGEST_PHOTO_MAX_PENDING_BYTES` (default 256 МиБ) — бюджет байт
  выполняющихся и ожидающих кадров (`len(payload)`). Сверх бюджета кадр
  отбрасывается с WARNING и `outcome="dropped_overload"`. Значение меньше
  `MAX_MEDIA_TRANSFER_BYTES` + 20 — ошибка старта.

### Dispatcher drain-fast + bounded concurrency

Прежний `OutboundDispatcher` делал `asyncio.sleep(2s)` после *каждого* батча,
ограничивая пропускную способность ~33–47 событий/с независимо от длины очереди.
Исправлено: сон только при пустом батче (poll_interval = 2s без нагрузки);
непустые батчи доставляются back-to-back. Внутри батча — ограниченный параллелизм:
`OUTBOUND_DISPATCHER_CONCURRENCY` (default 6) параллельных POST в `history_api`.

### Измеренный потолок

| Сценарий | Метрика |
|---|---|
| DB ops/s, одиночный процесс | ~600 (при pool=20, ingest_concurrency=10) |
| Комфортный масштаб (stat раз в 300 с) | ~10 000 реле |
| Одиночный-процессный потолок (агрессивные stat) | ~18–20 000 реле |
| За пределами → | горизонтальное масштабирование locker_api |

## Переменные окружения

- `DB_HOST` - Хост БД (default: localhost)
- `DB_PORT` - Порт БД (default: 5432)
- `DB_USER` - Пользователь БД (default: postgres)
- `DB_PASSWORD` - Пароль БД (default: postgres)
- `DB_NAME` - Имя БД (default: scud)
- `APP_PORT` - Порт приложения (default: 8001)
- `AUTH_API_URL` - URL Auth API для step-up issue/verify (default: `http://auth_api:8003`)
- `INTERNAL_STEP_UP_HMAC_KEY` - HMAC-ключ для подписи step-up запросов в auth_api (см. «2FA-подтверждение команд»)
- `HISTORY_API_URL` - URL History API для публикации security events, включая `command_2fa_mismatch` (default: `http://history_api:8002`)
- `COMMAND_QUEUE_PROCESSOR_INTERVAL_S` - Интервал поллинга БД-очереди команд в секундах (default: 5; в `.env.fast` понижен для E2E)
- `COMMAND_TTL_S` - Срок жизни команды от создания в секундах (default: 60; применяется ко всем типам, включая `reboot`). Значение ≤ 0 валит старт; значение больше `COMMAND_ACK_TIMEOUT_MINUTES`·60 даёт WARNING

## Запуск

```bash
cd backend
docker compose up locker_api
```

Локальный запуск без Docker не поддерживается: backend-сервисы зависят от
пакетов из сертифицированного `main` Astra Linux apt, которые не
устанавливаются через pip из PyPI. Все тесты тоже выполняются только в Docker:

```bash
cd backend
docker compose --profile tests up --build locker_api_tests
```

## Интеграция с другими подсистемами

- **User API** — использует таблицу `users` для валидации `created_by` / `updated_by`.
- **Notification API** — отправка уведомлений при нарушении порогов и изменениях
  состояния шкафа (см. `app/services/notification_client.py` и `state_monitor`).
- **History API** — запись событий безопасности, телеметрии, истории команд и
  медиа-файлов через персистентную очередь `outbound_history_events` и
  `OutboundDispatcher` (см. секцию «History Producers» выше).

## Состояния шкафа

Состояние шкафа описывается тремя ортогональными полями:

### connectivity (связность)
- `online` — шкаф на связи (реле публикует `stat` через MQTT)
- `offline` — шкаф не на связи

Управляется автоматически MQTT-ингестом: `online` — на каждый принятый `stat`;
`offline` — либо явный `stat=0`, либо `offline_monitor` по таймауту
`MQTT_OFFLINE_TIMEOUT_S` (см. секцию «MQTT relay transport» выше). Переход в
`offline` **не сбрасывает** `door`.

### door (состояние двери)
- `closed` — дверь закрыта
- `open` — дверь открыта

Управляется результатами выполнения команд `open`/`close`. Сохраняется между переподключениями.

При успешно выполненной команде `open` дополнительно обновляется поле `last_opened_at` (TIMESTAMPTZ) — денормализованный кэш времени последнего открытия для быстрых запросов карточки шкафа без обращения в `history_api`. Полная история открытий хранится в `history_api.command_history` (см. секцию «History Producers» ниже).

## History Producers

Locker API публикует данные в `history_api` через персистентную очередь
`outbound_history_events` (таблица в `scud_lockers`) и фоновый воркер
`OutboundDispatcher` (poll 2s, exp.backoff, idempotent). Прямых HTTP-вызовов
из горячего пути устройства нет — кратковременная недоступность `history_api`
не приводит к потерям.

| Продьюсер | Канал | Эндпойнт | Trigger |
|---|---|---|---|
| `TelemetryProducer` | `telemetry` | `POST /internal/api/v1/telemetry` | `mqtt/ingest.py._handle_stat` (после `current_value` update по данным `stat`). Подавляется при `door=OPEN`/`MAINTENANCE`. |
| `CommandHistoryProducer` | `command_history` | `POST /internal/api/v1/commands/history` | `LockerCommandHandler.post` (status=pending, sent — `app/handlers/command.py`), `mqtt/results.py` (executed/failed, по `rep`). Идемпотентно по `command_id`. |
| `OfflineEventProducer` | `security_event` | `POST /internal/api/v1/events/security` | `mqtt/ingest.py._emit_offline` (явный `stat=0`) или `mqtt/offline_monitor.py` (таймаут `MQTT_OFFLINE_TIMEOUT_S`) после `update_connectivity(OFFLINE)`. `metadata.reason` — единственное допустимое значение `heartbeat_timeout` (`_VALID_REASONS` в `offline_producer.py`); любое другое значение `publish_offline` отклоняет `ValueError`-ом. severity=medium. |
| `ThresholdViolationProducer` | `security_event` | `POST /internal/api/v1/events/security` | `state_monitor.handle_threshold_violation` для большинства датчиков. severity=high. |
| `IntrusionProducer` | `security_event` | `POST /internal/api/v1/events/security` | `state_monitor` при `sensor_type_code='intrusion'`; `mqtt/ingest.py._sync_door` на открытие двери без команды «Открыть»; также `mqtt/ingest.py._ensure_intrusion` на каждую relay-фото-серию (event_uid выводится из `series_uid`, в `metadata` — `relay_event_id`, `series_uid`, `photos_cnt`). severity=high. |
| `DoorTamperProducer` | `security_event` | `POST /internal/api/v1/events/security` | `state_monitor` при `sensor_type_code='tamper'`. severity=critical. |
| `CommandLateExecutionProducer` | `security_event` | `POST /internal/api/v1/events/security` | `mqtt/results.py` на `SUCCESS`-`rep` по уже терминальной команде (`metadata.late_kind`: `after_failure` / `replay`). Детерминированный event_uid (uuid5 от команды, вида и минутного окна приёма). severity=high. |
| `MediaProducer` | `media` | `POST /internal/api/v1/media` (multipart) | `mqtt/ingest.py._publish_frame` на каждый кадр топика `photos`, без задержки. `security_event_uid` = event_uid intrusion-события серии: history_api привяжет кадр, даже если событие доедет позже. |

Конфигурация ретеншена очереди: env `OUTBOUND_RETENTION_DAYS` (default 7).

## Relay-фото (MQTT `photos`)

Релейная плата присылает серию фото через топик `photos`: кадры `1..photos_cnt`,
каждый — самостоятельный JPEG (заголовок + тело — см. «MQTT relay transport»
выше и [`openapi/openapi_relay.md`](openapi/openapi_relay.md)). Кадры не
склеиваются и не буферизуются: каждый сразу ставится в outbox и становится
отдельной строкой `locker_media` в history_api.

Серии ведёт реестр `relay_photo_series` (`RelayPhotoSeriesRepository`,
логика классификации — `app/mqtt/photo_series.py`). Серия — это
`(locker, relay_id, event_id)` в пределах эпохи `RELAY_PHOTO_SERIES_EPOCH_S`
(default 24 ч); у неё случайный `series_uid`, из которого детерминированно
выводятся event_uid intrusion-события и каждого кадра. Кадр классифицируется так:

- `new_series` — для event_id нет текущей серии: создаётся серия, эмитится
  `intrusion` security event;
- `new_frame` — такого photo_id в свежей серии ещё не было: кадр публикуется;
- `duplicate` — тот же кадр (sha256 без хвостового паддинга `0x00`/`0xFF`)
  уже есть в свежей серии: повторная доставка, ничего не публикуется;
- `reset` — другой кадр на занятом photo_id или серия старше эпохи: плата
  переиспользовала event_id, старая серия помечается `superseded_at`,
  начинается новый инцидент.

Кадр без JPEG SOI (`FFD8`), с `photo_id`/`photos_cnt` вне 1..16 или сверх
`RELAY_PHOTO_SERIES_MAX_BYTES` на серию (default `MAX_MEDIA_TRANSFER_BYTES`) —
`dropped_malformed`. Новых серий на одну плату не больше
`RELAY_PHOTO_SERIES_RATE` в секунду с запасом `RELAY_PHOTO_SERIES_BURST`
(default 0.1 и 10), сверх — `dropped_rate_limited`. Распределение кадров —
метрика `scud_relay_photo_frames_total{disposition}`.

Параллельные кадры одной серии дают одну строку `relay_photo_series`, один
intrusion в outbox, один `new_series` и один токен rate-limit: классификация и
создание серии сериализованы на (шкаф, relay, `event_id`), поэтому второй кадр
видит уже созданную серию и считается `new_frame`, даже если токенов больше
нет. Intrusion ставится в outbox одной командой вместе с отметкой
`intrusion_enqueued_at`. Если создание серии упало, токен возвращается.

Если intrusion не удалось поставить в outbox, его повторяют на дубликате кадра
и фоновый цикл (`RELAY_PHOTO_MAINTENANCE_INTERVAL_S`, default 60 с) для серий
старше минуты. Тот же цикл удаляет серии без кадров дольше
`RELAY_PHOTO_SERIES_RETENTION_DAYS` (default 90).

Полные схемы сообщений — [`openapi/openapi_relay.md`](openapi/openapi_relay.md).

### operational_status (операционный статус)
- `normal` — штатный режим
- `maintenance` — на обслуживании (оператор)
- `alarm` — тревога (StateMonitor)

Управляется оператором (через PUT `/locker/{id}/state`) и автоматически StateMonitor.

### Гарды StateMonitor

При нарушении порогового значения StateMonitor проверяет гарды перед переводом в `alarm`:

1. **maintenance** — если шкаф на обслуживании, тревога подавляется
2. **door + open** — если сработал дверной датчик при легально открытой двери, тревога подавляется
3. **offline** — если шкаф офлайн, тревога подавляется (некому доставить команду)

## Типы датчиков

- `temperature` - Температура (°C)
- `humidity` - Влажность (%)
- `shock` - Удар/вибрация
- `power` - Питание (V)
- `intrusion` - Взлом (boolean; true — дверь открыта без команды «Открыть»)
- `door` - Состояние двери (open/closed)

## Статусы команд

- `pending` - Ожидает отправки
- `sent` - Отправлена на устройство
- `delivered` - Доставлена устройству
- `executed` - Выполнена устройством
- `failed` - Ошибка выполнения (в т.ч. истёк `COMMAND_TTL_S`)

`executed` и `failed` терминальны и больше не меняются.

## 2FA-подтверждение команд

Для пользователей с `users.require_2fa = true` привилегированные команды
(`open`, `reboot`, `config`) требуют step-up OTP-подтверждения. Команда
`close` всегда идёт обычным однозвенным flow — реверс заблокированной двери
после ошибки не должен зависеть от Max-бота.

### Триггер

ACM в ответ на `POST /acm/check` пробрасывает заголовок
`X-Remote-Require-2FA: true`, если пользователь с `require_2fa=true` шёл
на `POST /locker/{id}/commands`. Apache Lua форвардит заголовок в
locker_api. Locker_api сверяет тело запроса (`type ∈ OTP_REQUIRED_COMMANDS`
из `shared.access_control.commands`) и принимает решение, нужен ли step-up.

### Двушаговый flow (HTTP 202 → POST /commands/confirm)

1. **`POST /api/v1/locker/{id}/commands`** с `X-Remote-Require-2FA: true`
   и `type ∈ {open, reboot, config}` отвечает `202 Accepted` с
   `{challenge_id, expires_at, delivery_status}`. Команда **НЕ** записана в
   БД — это лишь приглашение к подтверждению. OTP-код в это время
   доставляется в Max-бот через `notification_api → MaxBotPoller`.

   - Если `users.max_user_id` пуст — ответ `401` с
     `ProblemType.MAX_BINDING_REQUIRED` и подсказкой пользователю обратиться
     к админу за invite-ссылкой.
   - Если `auth_api` недоступен — `503`.

2. **`POST /api/v1/locker/{id}/commands/confirm`** с
   `{challenge_id, code}` и теми же заголовками, что и `POST /commands`.
   После успешного `step-up/verify` в auth_api команда из исходного
   запроса попадает в очередь идентично однозвенному 201-flow.

   - Сервер повторно сверяет матрицу `command_type × role` и совпадение
     `locker_id` в challenge с URL — защита от reuse challenge на другом шкафу.
   - Неверный код / просроченный challenge / уже использованный — `401`.

Без `X-Remote-Require-2FA` (или с `command_type = close`) поведение
неизменно: `POST /commands` сразу возвращает `201` с записью в `locker_commands`.

### Связанные сервисы и секреты

- `AUTH_API_URL` (default `http://auth_api:8003`) — куда locker_api ходит
  за issue/verify.
- `INTERNAL_STEP_UP_HMAC_KEY` — общий с auth_api ключ HMAC. Подписывает
  исходящие `/internal/api/v1/step-up/{issue,verify}`. Пустой → запросы
  идут unsigned (только dev).
- Security events `command_2fa_issued / verified / failed / mismatch`
  публикуются auth_api (issue/verify) и locker_api (mismatch) в history_api
  через `OutboundDispatcher`.

Полный контракт endpoints (включая schemas ответов и коды ошибок) —
`openapi/openapi.yaml` (раздел `commands`) и `openapi/paths/commands.yaml`.

## Примеры использования

### Создание шкафа
```bash
curl -X POST http://localhost:8001/api/v1/lockers \
  -H "Content-Type: application/json" \
  -H "X-Remote-User: 1" \
  -d '{
    "inventory_number": "СО №777",
    "location_address": "ул. Ленина, 1",
    "location_lat": 55.751244,
    "location_lon": 37.618423
  }'
```

### Отправка команды на открытие
```bash
curl -X POST http://localhost:8001/api/v1/locker/{locker_id}/commands \
  -H "Content-Type: application/json" \
  -H "X-Remote-User: 1" \
  -d '{
    "type": "open",
    "parameters": {}
  }'
```

### Создание порогового значения
```bash
curl -X POST http://localhost:8001/api/v1/locker/{locker_id}/sensors/{sensor_id}/thresholds \
  -H "Content-Type: application/json" \
  -H "X-Remote-User: 1" \
  -d '{
    "min_value": -10,
    "max_value": 50,
    "description": "Допустимая температура от -10°C до +50°C"
  }'
```

## Разработка

См. [DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md) для детальной информации о разработке и архитектуре.
