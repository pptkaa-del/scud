# CHANGELOG

Формат основан на Keep a Changelog: версии идут сверху вниз, в каждой
секции — подразделы Added / Changed / Fixed / Removed по необходимости.

История изменений до 2026-04-28 в этом файле не фиксировалась — её следует
восстанавливать из `git log`.

## 2026-11-17 — SMS-доставка OTP через Yeastar TG200

PR #72. OTP для входа и step-up уходит по SMS через шлюз Yeastar TG200, при
неудаче — по Max (SMS-primary, MAX-fallback).

### Added
- Профиль compose `sms` и сайдкар `wg_gateway` (WireGuard-туннель к шлюзу).
- Четыре секрета `sms_api_user`, `sms_api_password`, `wg_private_key`,
  `wg_preshared_key`; `deploy.sh` создаёт их пустыми (пусто — SMS выключен).
- Метрика `scud_otp_delivery_total{service,channel,outcome}`.
- Блок схемы `outbound-max-channel` (`notification_api`, `001_init.sql`):
  колонки `outbound_max_messages.channel`/`address`.

### Changed
- `POST /api/v1/auth/login` отвечает 503, если код не доставлен ни одним
  каналом (раньше 401 `max-binding-required`).
- Step-up: OTP доставляется по SMS, при неудаче — по Max; если ни один
  канал не доставил код сразу, выдача, как и раньше, отвечает 201
  `queued_until_binding`.

### Обновление существующих установок
Блок `outbound-max-channel` применяет `deploy.sh update` (или `migrate`);
вручную — по `backend/DEPLOYMENT.md`, §6. SMS включается ответом на вопрос
установки «SMS-шлюз Yeastar (WireGuard)», см. раздел «SMS-шлюз».

## 2026-11-06 — Скрипт установки и обновления

Установка, обновление, откат и переустановка сервера сведены в один скрипт;
изменения схемы для существующих БД оформлены именованными блоками, которые
скрипт применяет сам.

### Added
- `backend/scripts/deploy.sh`: команды `install`, `update`, `reinstall`,
  `rollback`, `backup`, `migrate`, `status`, `pack`. Генерация секретов и
  `.env`, сборка образов до остановки сервисов, дамп всех БД на хост перед
  `update`/`reinstall`, откат на предыдущие код и образы, архив релиза
  (`pack`, при желании с `images.tar`). Руководство — `backend/DEPLOYMENT.md`,
  раздел «Автоматическая установка и обновление».
- Журнал блоков схемы `deploy_schema_upgrades` в каждой из 6 БД; блоки
  `outbox-dead-letter`, `relay-photo-series` (`locker_api`),
  `media-security-event-uid` (`history_api`), `outbound-max-body-nullable`
  (`notification_api`) в `001_init.sql` (рядом с уже имевшимися
  `sensor-type-states` и `relay-control`). Правила — `backend/CONVENTIONS.md`.
  На установке без журнала `sensor-type-states` только отмечается, без
  повторного выполнения, если его результат уже есть в БД (у типа `intrusion`
  заполнены `states`); иначе выполняется.
- `backend/scripts/tests/test_deploy.sh`, `test_schema_upgrade.sh` и шаги CI
  `deploy.sh unit tests`, `schema upgrade blocks` в job `lint`.
- `init-scripts/init-user-db.sh`: при `SKIP_MIGRATIONS=1` пропускает
  `001_init.sql` и только заново выставляет роль и права `acm_ro` (используется
  при обновлении).

### Changed
- `backend/DEPLOYMENT.md`: новый первый раздел про `deploy.sh`; §6 ссылается
  на блоки схемы вместо ручного DDL; раздел «Frontend deployment» — каталог
  `frontend/` из репозитория; порт Zabbix web — 8091.
- `docker-compose.yaml`: `ADMIN_PHONE` у `user_api` берётся из окружения
  (`${ADMIN_PHONE:-+79000000000}`).
- `frontend/public/script.js`: `API_BASE_URL = '/api/v1'` (тот же origin).

### Fixed
- `APP_ENV` из `.env` не доходила до контейнеров (compose её не передавал), и
  fail-closed проверка секретов в production молча не включалась: теперь
  `APP_ENV=${APP_ENV:-development}` задана у `user_api`, `auth_api`,
  `locker_api`, `history_api`, `notification_api`, `reports_api`, `acm`.
- Инструкция `chmod 600 secrets/*` приводила к crash-loop postgres
  (bind-mount читается под его uid): теперь файлы 0644, каталог `secrets/` 0700.
- В `frontend/public/script.js` был прописан адрес стенда.

## 2026-11-05 — Управление блоком реле с сайта

### Added
- `locker_api`: команды `relay_on`/`relay_off`/`relay_restart` через
  `POST /locker/{id}/commands` с `parameters.relay` 1..4, без retain (слот
  `com/{id}` остаётся за замком). Политика канала проверяется одной функцией
  при создании команды и при подтверждении 2FA; одна незавершённая relay-команда
  на канал (частичный уникальный индекс `ux_locker_commands_relay_active`).
- Состояния каналов `r1..r4` из `stat` пишутся в `locker_relays`; у шкафа
  появились `has_relays` и `board_version`. Новые `GET` и `PATCH /locker/{id}/relays`,
  правила ACM, OpenAPI.

### Обновление существующих установок
Блок `relay-control` (`scud_lockers`) применяет `deploy.sh update`; вручную —
`backend/DEPLOYMENT.md` §6. Блок выполняется до
`docker compose up -d --build locker_api`: без него `GET /locker/{id}/relays`
отвечает 500, а relay-команды не создаются. Выполнять **без `-1`**: значения
enum, добавленные `ALTER TYPE … ADD VALUE`, нельзя использовать в той же
транзакции (`unsafe use of new value`), а индекс на них ссылается. Строки
каналов появятся после первого `stat` платы с блоком реле.

Проверка (ожидается `relay_on,relay_off,relay_restart|2|1`):
```bash
docker compose exec -T postgres_locker psql -U admin -d scud_lockers -At -c \
    "SELECT (SELECT string_agg(e::text, ',') FROM unnest(enum_range(NULL::command_type)) e WHERE e::text LIKE 'relay_%'),
            (SELECT count(*) FROM information_schema.columns WHERE table_name = 'lockers' AND column_name IN ('board_version', 'has_relays')),
            (SELECT count(*) FROM pg_indexes WHERE indexname = 'ux_locker_commands_relay_active');"
```
Повторный запуск безопасен (всё `IF NOT EXISTS`); если прогон оборвался на
середине — повторите.

Откат: предыдущий образ `locker_api` новые колонки и таблицу игнорирует;
значения enum удалить нельзя, их не трогайте.

## 2026-10-13 — Взлом: открытие двери без команды

Плата без камеры о несанкционированном открытии не сообщает (протокол §7.1 —
только серией фото), поэтому открытие двери без команды не давало ни события,
ни значения датчика «Взлом».

### Fixed
- `locker_api`: открытие двери при действующей команде замка не «Открыть»
  порождает `intrusion` security event (high) и выставляет датчик `intrusion`
  в true; закрытие двери сбрасывает его в false. Шкаф в `normal` переводится в
  `alarm`, операторам и администраторам уходит постоянное уведомление; в
  `maintenance` тревоги нет, шкаф в `alarm` до сброса оператором новых
  уведомлений не шлёт — событие пишется всегда.
- Действующая команда замка — последняя опубликованная в `com/{id}` OPEN/CLOSE
  (по `sent_at`, не позже приёма `stat`): плата держит её retained и выполняет
  при переподключении, поэтому решение не зависит от `rep`. Не считаются
  команды, отвергнутые платой (`rep` FAILURE / NOT_SUPPORTED); отказ, пришедший
  после истечения TTL, заменяет причину ошибки команды.
- Прежнее состояние двери — значение датчика `door`, которое пишет только
  `stat` (в том числе в `MAINTENANCE`): `lockers.door` обновляет и командный
  путь. Первый `stat` нового шкафа тревогу не поднимает; `stat`, пришедший
  раньше уже применённого, дверь не трогает.

### Changed
- Тип `intrusion` получил `states` («Норма»/«Взлом») в блоке
  `sensor-type-states`; на существующей БД блок перезапускается до пересборки
  `locker_api` (см. «Обновление существующих установок» в записи 2026-10-05).
- Плата с камерой даёт на одно вскрытие два `intrusion`: по `stat` (сразу) и
  по серии фото (с кадрами).

## 2026-10-05 — Состояния перечислимых датчиков в БД

Словарь подписей перечислимых датчиков переехал из кода в колонку
`sensor_types.states` (JSONB) и редактируется через API.

### Changed
- `locker_api`: `sensor_types.states` — массив `{value, label, severity}`;
  `POST`/`PUT /sensor-types` принимают `states` (в `PUT` — по наличию ключа:
  нет — без изменений, `null` — очистка, массив — замена), `400` на
  невалидные, `409` на конфликт с активными порогами. `display` датчика
  считается из `states` типа при каждом чтении; пороги `min`/`max` для таких
  датчиков запрещены, `fixed_value` — из `states`.
- Дефолты `power`/`net_type`/`error`/`door` сеются идемпотентным блоком
  `sensor-type-states` в `001_init.sql`; на существующей БД блок выполняется
  до пересборки `locker_api` (см. «Обновление существующих установок» ниже).

### Removed
- `locker_api`: реестр `sensor_registry.py`, «управляемые» типы и признак
  `managed` в `GET /sensor-types`, стартовая синхронизация типов, `409` на
  правку/удаление `power`/`net_type`/`error`.

### Обновление существующих установок
Блок `sensor-type-states` (`scud_lockers`) применяет `deploy.sh update`;
вручную — `backend/DEPLOYMENT.md` §6. Блок выполняется до
`docker compose up -d --build locker_api`: без колонки `sensor_types.states`
чтение и создание датчиков, пороги и эндпоинты `/sensor-types` отвечают 500
(MQTT-приём и тревоги продолжают работать).

Проверка (ожидается 5 строк, во всех `has_states = t`):
```bash
docker compose exec -T postgres_locker psql -U admin -d scud_lockers -c \
    "SELECT code, states IS NOT NULL AS has_states FROM sensor_types WHERE code IN ('power','net_type','error','door','intrusion') ORDER BY code;"
```
Повторный запуск безопасен; правки администратора не затираются (`UPDATE`
только при `states IS NULL`). Чтобы вернуть дефолт типа — `PUT /sensor-types/{id}`
с `"states": null`, затем повторить блок. На установке без журнала при уже
существующей колонке блок только отмечается, без выполнения (иначе вернул бы
удалённые администратором типы `net_type`/`error`).

Откат: предыдущий образ `locker_api` колонку `states` игнорирует, поэтому
колонку не удаляйте.

## 2026-10-04 — Relay-фото: один кадр — одно медиа

Плата присылает в топик `photos` серию самостоятельных JPEG (`photo_id`
1..`photos_cnt`), а `locker_api` склеивал кадры в один буфер и отдавал его как
одно «фото»: в карусели инцидента был один битый файл вместо серии. Теперь
каждый кадр — отдельная запись `locker_media`.

### Changed
- `locker_api`: `PhotoSeriesBuffer` заменён реестром серий `relay_photo_series`
  (`app/mqtt/photo_series.py`, `RelayPhotoSeriesRepository`). Каждый кадр сразу
  ставится в outbox без 15-секундной задержки; серия — `(шкаф, relay_id,
  event_id)` в пределах `RELAY_PHOTO_SERIES_EPOCH_S` (24 ч). Повтор кадра
  распознаётся по sha256 без хвостового паддинга, переиспользование `event_id`
  платой начинает новый инцидент. Intrusion-событие эмитится по первому кадру,
  при сбое — повторяется на дубликате и фоновым циклом. Параллельные кадры
  новой серии ставят в outbox один intrusion и считаются одним `new_series` с
  одним токеном rate-limit: захват серии и вставка в outbox — одна команда, а
  классификация кадра сериализована на серию, так что при последнем токене
  второй кадр не теряется.
- `history_api`: `POST /internal/api/v1/media` принимает `security_event_uid` и
  привязывает кадр к событию по нему в любом порядке доставки (при записи
  события висящие кадры дотягиваются). Окно ±30 с осталось только для legacy и
  больше не матчит события relay-серий. `GET /api/v1/events/security/{id}/media`
  отдаёт кадры по `metadata.photo_id`, legacy-записи — последними.
- Кадры `photos` идут отдельной полосой bulkhead с бюджетом байт
  (`MQTT_INGEST_PHOTO_MAX_CONCURRENCY`, `MQTT_INGEST_PHOTO_MAX_PENDING_BYTES`);
  новые серии ограничены per relay (`RELAY_PHOTO_SERIES_RATE`/`_BURST`, исход
  `dropped_rate_limited`). Кадр без JPEG SOI — `dropped_malformed`.
- Метрика `scud_relay_photo_frames_total{disposition}`; в Zabbix — item
  сбросов серий у `locker_api` и `scud.dbprobe[pg,postgres_history,history_orphan_media]`
  (кадры без события дольше 10 минут) с WARNING-триггером через 30 минут.
- Эмулятор: `--photos-per-incident` (по умолчанию 5 различных кадров на
  инцидент); system E2E проверяет 5 медиа и отсутствие дублей при повторе кадра.

### Обновление существующих установок
Блоки `media-security-event-uid` (`scud_history`), `outbox-dead-letter` и
`relay-photo-series` (`scud_lockers`) применяет `deploy.sh update`; вручную —
`backend/DEPLOYMENT.md` §6. Старые склеенные фото-записи не мигрируются и в
старых инцидентах остаются одним фото.

Порядок обязателен: блоки на обе БД → `history_api` → `locker_api`. Новый
`locker_api` против старого `history_api` теряет привязку кадров (поле
`security_event_uid` молча игнорируется). Без DDL новый `history_api` отвечает
500 на каждое событие безопасности с `event_uid` и на каждое медиа (outbox
ретраит до dead-letter), а новый `locker_api` теряет intrusion релейных плат:
событие создаётся только через `relay_photo_series`. Новые env
(`MQTT_INGEST_PHOTO_*`, `RELAY_PHOTO_SERIES_*`) задавать не обязательно.

Проверки (команды — из `backend/`):

1. До выкатки — запомнить число недоставленных media в outbox:
   ```bash
   docker compose exec -T postgres_locker psql -U admin -d scud_lockers -c \
       "SELECT count(*) FROM outbound_history_events
        WHERE channel = 'media' AND delivered_at IS NULL AND dead_lettered_at IS NULL;"
   ```
2. После старта — пропущенный DDL виден сразу (фоновая чистка серий пишет
   `relay photo pending-intrusion sweep failed`, `history_api` на первое
   событие или медиа — `UndefinedColumn`); вывод должен быть пустым:
   ```bash
   docker compose logs --since 5m locker_api history_api \
       | grep -E "relay photo (pending-intrusion sweep|series purge) failed|UndefinedColumn|UndefinedTable"
   ```
3. Legacy-media (поставленные старым кодом с задержкой 15 с) должны за секунды
   дойти до 0 в `legacy`; кадры без события дольше 10 минут — 0:
   ```bash
   docker compose exec -T postgres_locker psql -U admin -d scud_lockers -c \
       "SELECT count(*) FILTER (WHERE payload->'form_fields' ? 'event_sensor_id') AS legacy,
               count(*) FILTER (WHERE payload->'form_fields' ? 'security_event_uid') AS series
        FROM outbound_history_events
        WHERE channel = 'media' AND delivered_at IS NULL AND dead_lettered_at IS NULL;"
   docker compose exec -T postgres_history psql -U admin -d scud_history -c \
       "SELECT count(*) FROM locker_media
        WHERE security_event_id IS NULL AND security_event_uid IS NOT NULL
          AND deleted_at IS NULL AND uploaded_at < now() - interval '10 minutes';"
   ```
4. Профиль `monitoring`: агент пересобрать только после DDL `history_api`; до
   него метрика `history_orphan_media` отдаёт 0 вместо числа зависших кадров:
   ```bash
   docker compose --profile monitoring up -d --build zabbix_agent2
   ```

Откат: вернуть на предыдущий образ только `locker_api`; `history_api` оставить
новым — он совместим со старым `locker_api` и привяжет по `security_event_uid`
кадры, которые ещё стоят в outbox. Колонку и таблицу можно оставить — старый
код их не читает. Если `history_api` всё же нужно откатить, сначала дождаться,
пока запрос п. 3 покажет `series` = 0 и в outbox не останется недоставленных
строк канала `security_event`: старый `history_api` сохраняет кадр без
`security_event_uid`, и связь с событием теряется навсегда. При откате ниже
«TTL команд» — см. откат в записи «TTL команд шкафа» ниже по журналу.

## 2026-10-04 — TTL команд шкафа и неизменяемость терминального статуса

Команда, отправленная на офлайн-шкаф, висела в очереди часами и
срабатывала после переподключения платы; поздний или повторный `rep`
перезаписывал уже завершённую команду и заново создавал уведомление
«Шкаф открыт».

### Fixed
- `locker_api`: команда без результата от шкафа за `COMMAND_TTL_S`
  (по умолчанию 60 с, все типы, включая `reboot`) переводится в `failed`
  с причиной «истёк срок ожидания ответа шкафа» одним SQL-statement вместе
  с outbox-событием `command_history`; просроченная команда больше не
  выбирается и не отправляется. Очистка — на каждом тике queue-processor'а
  и батчами при старте.
- `locker_api`: `executed`/`failed` терминальны — переход только из
  `pending`/`sent`/`delivered` (CAS). Поздний или повторный `rep` учитывается
  как `scud_mqtt_relay_messages_total{outcome="stale_rep"}` и не трогает
  статус, дверь и уведомления.
- `locker_api`: при оборванной сессии с MQTT-брокером команда не
  публикуется и не помечается `sent` (gmqtt не бросал, а копил пакет до
  реконнекта); claim возвращается в `pending` для повтора в пределах TTL.
- `locker_api`: `sent` в историю команд публикуется только после
  фактической отправки (раньше inline-путь публиковал его и при сбое, а
  queue-processor не публиковал вовсе); неудачная отправка возвращает
  команду в `pending` для повтора в пределах TTL.
- `history_api`: upsert `command_history` при смене одного терминального
  статуса на другой больше не дописывает `executed_at`/`failed_at`,
  `result` и `error_message`.

### Added
- Событие безопасности `command_late_execution` (severity `high`):
  `SUCCESS` по уже `failed` команде (`late_kind=after_failure`) или повтор
  `SUCCESS` позже TTL после `executed` (`late_kind=replay`). Повтор по
  выполненной `close` не тревожит: retained `CLOSE` штатно переисполняется
  платой при каждой переподписке на `com/{id}`.
  Детерминированный `event_uid` схлопывает дубли одного `rep`; фильтр
  `GET /api/v1/events/security?event_type=command_late_execution`.
- Постоянное уведомление операторам и администраторам о позднем
  исполнении («Шкаф … выполнил команду «открыть» с опозданием — дверь могла
  открыться без оператора»), не больше одного на `rep`.
- Метрики: `scud_command_lifecycle_total{transition="ttl_expired"}`,
  `scud_mqtt_relay_messages_total{outcome="stale_rep"}`.
- Env `COMMAND_TTL_S` (compose, `.env.example`, `.env.fast`).

### Removed
- `CommandRepository.update_status_to_sent_batch` — вызовов не было.

### Известные ограничения
- Retained `com` не очищается: переподключившаяся плата может выполнить
  просроченный `OPEN` — это фиксируется как `command_late_execution`, но
  дверь открывается.

### Обновление существующих установок
Своего DDL нет. Порядок выкатки — сначала `history_api` (или оба одной
командой): тип `command_late_execution` живёт в копии `shared.enums` каждого
образа, и старый `history_api` отвечал бы на такие события 400 до dead-letter.
`deploy.sh update` соблюдает порядок; вручную:
```bash
docker compose up -d --build history_api && docker compose up -d --build locker_api
```
Если в выкатку входит «Relay-фото: один кадр — одно медиа», сначала его DDL.

Разовый эффект: на первом старте `locker_api` переводит ВСЕ незавершённые
команды старше TTL в `failed` («истёк срок ожидания ответа шкафа»), по одному
outbox-событию истории на каждую. Посмотреть их до выкатки:
```bash
docker compose exec postgres_locker psql -U admin -d scud_lockers -c \
    "SELECT status, count(*), min(created_at) FROM locker_commands
     WHERE status IN ('pending','sent','delivered') GROUP BY 1;"
```
После старта тот же запрос с `AND created_at < now() - interval '2 minutes'`
должен вернуть 0 строк.

Откат: вернуть на предыдущий образ только `locker_api`; `history_api` оставить
новым — он совместим со старым `locker_api`. Команды, уже переведённые в
`failed` по TTL, остаются `failed`. Старый `history_api` не знает тип
`command_late_execution`: список событий безопасности, в страницу которого
попадает такая запись, и её карточка отвечают 500, а недоставленные события
этого типа из outbox получают 400 и уходят в dead-letter. Если `history_api`
всё же нужно откатить — сначала дождаться пустого outbox канала
`security_event`, затем перетипировать записи (тип в журнале теряется,
исходный остаётся в `metadata`):
```bash
docker compose exec -T postgres_locker psql -U admin -d scud_lockers -c \
    "SELECT count(*) FROM outbound_history_events
     WHERE channel = 'security_event' AND delivered_at IS NULL AND dead_lettered_at IS NULL;"
docker compose exec -T postgres_history psql -U admin -d scud_history -c \
    "UPDATE security_events
     SET event_type = 'threshold_violation',
         metadata = COALESCE(metadata, '{}'::jsonb)
                    || '{\"original_event_type\": \"command_late_execution\"}'::jsonb
     WHERE event_type = 'command_late_execution';"
```

## 2026-10-04 — Подписи перечислимых датчиков релейной платы

### Added
- Поле `display: {label, severity}` у датчика (`GET/PATCH /locker/{id}/sensors…`,
  201 на `POST`): подпись сырого `current_value` для перечислимых кодов —
  `power` (От сети / От батареи), `net_type` (Ethernet / GSM / Не определён),
  `error` (Норма / Ошибка), `door` (Закрыта / Открыта). Для прочих кодов `null`.
  Словарь — `locker_api/app/sensor_registry.py`; `GET /sensor-types` отдаёт его в
  `states` и признак `managed`.
- Датчики `relay-net_type` (тип канала связи из `stat` v2) и `relay-error`
  (`stat == 2`). `error` пишется только при изменении, в том числе в
  `MAINTENANCE` и при исчерпанном rate-limit телеметрии; при `stat=0` хранит
  последнее значение.
- Стартовая синхронизация управляемых типов `power`/`net_type`/`error` в
  `sensor_types` (идемпотентный upsert, `name` не перезаписывается); seed
  `001_init.sql` дополнен `net_type` и `error`. Блоки схемы — `backend/DEPLOYMENT.md` §6.
- Пороги перечислимых датчиков: `min_value`/`max_value` → `400`, `fixed_value`
  числового типа канонизируется в целое из словаря.

### Changed
- Тип `power`: единица `V` убрана, описание исправлено (это источник питания
  0/1, а не напряжение).
- `PUT /sensor-types/{id}` кодовых полей и `DELETE` управляемого типа → `409`
  (409 на `DELETE` типа, на который ссылаются датчики, теперь задокументирован).
- Security-событие перехода платы в `ERROR` несёт `sensor_id` датчика `error`
  (тип и severity события прежние).

### Fixed
- `openapi_relay.md` §7 описывал только `battery`/`signal` — теперь таблица
  всех датчиков релейной платы.

## 2026-09-28 — `stat` v2 без блока реле: читать `stat` и `sl`

### Fixed
- `decode_stat` (`locker_api/app/mqtt/relay_codec.py`) при `relay_en=0`
  отбрасывал хвост кадра целиком, считая его нулевым дополнением. Реальная
  плата и эталонный RelayEmulator присылают 52 байта с заполненными `stat` и
  `sl` (обнуляются/не значимы только `r1..r4`), поэтому у плат без реле не
  писался уровень сигнала, не подсвечивался `stat=2` (ошибка) и не
  отрабатывал `stat=0`. Теперь при длине ≥ 52 байт `stat`/`sl` читаются,
  `r1..r4` по-прежнему игнорируются; 28-байтовый кадр принимается как раньше.

## 2026-08-29 — Миграция system E2E на релейный MQTT-двойник

После удаления WS-транспорта устройств десять файлов `tests/system/scenarios/`
безусловно скипались — фикстура `emulator` больше не имела носителя. Сценарии
портированы на `RelayRunner` (headless `scud_relay` поверх MQTT), покрытие
командного пути, 2FA, уведомлений, отчётов, tamper/media, типов значений
датчиков, переходов состояний и пороговых алярмов восстановлено.

### Changed
- 7 файлов `tests/system/scenarios/` переведены с фикстуры `emulator` на
  `RelayRunner`: `test_command_2fa.py`, `test_open_close_notifications.py`,
  `test_reports.py`, `test_tamper_media.py`, `test_sensor_value_types.py`,
  `test_state_transitions.py`, `test_alarm_thresholds.py`.
- `RelayRunner` дополнен `publish_photo()` — публикацией одиночной фото-серии
  в топик `photos` для сценариев доказательства взлома.
- Удалён мёртвый coerce-слой телеметрии (`locker_api/app/locker_data/db/repositories/sensor.py`,
  `telemetry_metrics.py`) вместе с метрикой `scud_telemetry_coerce_total` —
  вызовов из бизнес-логики не было, слой был живым только в юнит-тестах,
  писавшихся под удалённый WS-хендлер.
- MQTT закреплён единственным транспортом устройств: убраны недостижимые
  `REASON_NORMAL_CLOSE`/`REASON_SUPERSESSION` (`offline_producer.py`) и
  `MediaKind.VIDEO`; enum `media_kind` в схеме `history_api` (`001_init_history.sql`)
  сужен до `photo`, синхронизирован `openapi.yaml`. Сужение затрагивает только
  свежие БД — на уже развёрнутых стендах enum остаётся прежним, это безвредно:
  производителя значения `video` там никогда не было.

### Removed

Проверки, которым в MQTT-транспорте релейной платы нет носителя:

- датчики `temperature`/`humidity`/`shock` — в кадре `stat` таких полей нет,
  релейный путь создаёт только `battery`, `signal`, `door`, `power`, `tamper`,
  `intrusion`;
- порог по `door` — заблокирован продуктом (sweep-предикат StateMonitor
  исключает `door` при открытой двери), недостижим by design, а не пробел
  тестов;
- `door_tamper` через пороговую модель — на релее `tamper` только носитель
  device-asserted события, `current_value` не пишется, StateMonitor его не
  видит;
- `reason='normal_close'` и `supersession` при offline — причины ушли вместе
  с WS-транспортом;
- видео как медиа-вложение — на релейной плате видео не существовало никогда.

Удалены целиком (обвязка WS-эмулятора без замены):
`tests/system/scenarios/test_telemetry.py`,
`tests/system/scenarios/test_boolean_thresholds.py`,
`tests/system/utils/emulator_client.py`,
`tests/system/utils/provisioning.py`, фикстура `emulator` в `conftest.py`.

`tests/system/scenarios/test_security_events_full.py` удалён отдельно: его
пороговый тест был перенесён в `test_relay_mqtt_e2e.py` (этот файл фикстуру
`emulator` никогда не использовал), затем при ревью признан строгим
подмножеством `test_alarm_thresholds.py` + `test_sensor_value_types.py` и
удалён — уникального покрытия не потеряно.

## 2026-08-27 — TLS-листенер MQTT (порт 8883)

### Added
- Sidecar `mqtt_tls` (haproxy 2.6.23 из сертифицированного Astra `main`,
  `backend/mqtt-tls/`): `bind :8883 ssl` → `mqtt:1883` plaintext внутри
  `backend_net`. Профиль compose `mqtt-tls` — обычный `up` его не поднимает.
- `backend/scripts/gen-mqtt-tls-certs.sh` — собственный CA (10 лет) и серверный
  лист (825 дней) с SAN; в плату заливается `ca/ca.crt`.
- `mqtt_tls_tests` — `haproxy -c` реальным бинарём, TLS 1.2 хендшейк и все 16
  объявленных cipher-сюит; шаг `mqtt-tls` в job `lint`.
- Эмулятор: `scud relay run --tls [--cafile|--tls-insecure]`, порт по умолчанию
  8883 при `--tls`.

### Notes
- Транспортный контракт устройства и провижининг брокера не менялись: миграции
  и правки `openapi.yaml` не потребовались.
- Брокер по-прежнему собран `WITH_TLS=no`: `libssl-dev` отсутствует в
  сертифицированном `main`.

### Fixed
- `gen-mqtt-tls-certs.sh` отказывается работать, если `ca.crt` есть, а `ca.key`
  нет. Раньше в этом случае молча выпускался НОВЫЙ корневой CA поверх старого —
  плановая ротация серверного сертификата отключила бы весь парк прошитых плат.
  Отсутствие ключа штатно: его место — офлайн-хранилище.
- `FRONTEND_PATH` задан в job `gateway-tests`: монтировался несуществующий
  дефолт `/root/frontend`, статика отдавала 500 и 11 тестов падали.

### Fixed
- `aclfile` тома сверяется с дефолтами образа при каждом старте брокера
  (`backend/mqtt/acl-reconcile.sh`), сохраняя `user relay-*` блоки провижининга.
  Том сеялся из образа только при первом запуске, поэтому любая правка прав в
  образе молча не доезжала до уже развёрнутого брокера — так и вышло с правами
  на per-id топики. Пароль backend-пользователя сверялся так с INF-06, ACL — нет.

### Changed
- Приём телеметрии терпим к обеим схемам топиков: общим `stat`/`rep`/`photos` и
  per-id `stat/{id}`/`rep/{id}`/`photos/{id}` (ПИВ v2). Суффикс, если он есть,
  сверяется с `device_id` внутри кадра — расхождение отбрасывается как
  публикация от чужого имени. Провижининг выдаёт права на обе схемы, эмулятор
  умеет `--per-id-topics`.
- `rep` определяется по виду топика, а не точным сравнением: иначе `rep/{id}`
  терял приоритет в `IngestBulkhead` и ответы на команды сбрасывались под
  нагрузкой.

### Fixed
- Параметры TLS приведены к требованиям прошивки релейной платы: цепочка ровно
  из одного сертификата (второй не влезает в её приёмный буфер 1024 Б и рвёт
  рукопожатие), серверный лист не больше 1000 Б в DER, потолок протокола
  TLS 1.2 (TLS 1.3 прошивка не поддерживает), CA на 30 лет. Генератор
  сертификатов проверяет размер и длину цепочки и падает при нарушении.

## 2026-07-16 — Флаг 2FA в ответах API пользователей

`GET /api/v1/users` и `GET /api/v1/user/{id}` теперь возвращают флаг
`require_2fa` (статус включения 2FA) для каждого пользователя. Поле отражает
`user_auth.require_2fa` — тот же флаг, что переключают `POST
/user/{id}/enable-2fa` / `disable-2fa`; для пользователя без пароля / без
строки `user_auth` — `false`.

### Added

- **feat(user_api):** `require_2fa` в схеме `User` (`openapi/schemas/user.yaml`) —
  булево поле, всегда присутствует в ответе. Схема общая для `GET /user/{id}`,
  элементов `GET /users`, ответа `POST /users`, `PATCH /user/{id}` и
  `POST /user/{id}/reactivate`.

## 2026-07-15 — Список шкафов ограничен Допуском

**Изменение поведения API — требует внимания потребителей.** Раньше
`GET /api/v1/lockers` возвращал **все** шкафы любому аутентифицированному
пользователю, включая `worker`'а без единого Допуска. Теперь выдача ограничена
областью видимости вызывающего: видны только шкафы с активным Допуском.
Формулировка «Допуск» в `Architecture.md` уточнена — он управляет и видимостью
шкафа в списках, и правом его открыть (прежнее определение упоминало только
открытие, что и было корнем расхождения).

### Changed

- **`GET /api/v1/lockers`** — построчная фильтрация по Допуску. `worker` видит
  ровно шкафы с активным Допуском; `operator`, `administrator` и `worker` с
  полным доступом — все шкафы (поведение не изменилось). Фильтр применяется и к
  выборке, и к подсчёту, поэтому `total` считается по видимой области и
  соответствует `items`.
- **`POST /api/v1/lockers/batch`** — запрошенные `locker_ids` пересекаются с
  Допусками, чтобы шкаф вне области нельзя было получить в обход списка
  (на уровне ACM эндпоинт и так `operator+`; пересечение — второй рубеж).
- **Отсутствие Допусков — не ошибка:** оба эндпойнта отвечают `200` с пустым
  `items` (и `total: 0` для списка), а не `403`. Клиентам, различавшим «нет прав»
  по коду ответа, следует ориентироваться на пустую выдачу.

### Added

- **`shared.access_control.resolve_locker_scope(header, role) → LockerScope`** —
  единый разбор роли + `X-Remote-Lockers` в область видимости; `sql_filter_ids()`
  даёт `None` (без ограничения) либо список UUID для `id = ANY(%s::uuid[])`.
  Пустая область и отсутствие ограничения намеренно разведены — их схлопывание
  и есть fail-open. Предикат для одного шкафа (`require_locker_grant`) реализован
  через ту же область, поэтому список и точечный доступ решают одинаково.

## 2026-07-04 — Удаление WebSocket-транспорта устройств (переход на MQTT)

Транспорт устройств стандартизирован на **MQTT** (релейные платы через брокер
Mosquitto). Устаревший WebSocket-путь (Ed25519 challenge-response,
`/api/v1/device/ws`) полностью удалён из backend и эмулятора. Значения enum
`LockerTransport.ws` и `CommandType.config` оставлены **инертными** — только для
чтения ранее записанных строк (шкафы / история команд); новые ws-шкафы и
config-команды не создаются. Ветки `ws-removal-p1..p6`, PR #56/#57/#58/#59.

### Removed

- **WS device gateway** — `app/websocket/` (handler, mixins, роут
  `/api/v1/device/ws`), ACM-правило и Apache ProxyPass устройства (P1, #56).
- **WS-аутентификация** — эндпоинт `set-public-key`, Ed25519 challenge-response,
  метрики `ws_auth_*` / `ws_active_connections` / `ws_telemetry_dropped`, класс
  `WsAuthFailureReason` и env `WS_AUTH_*` / `WS_HEARTBEAT_*` (P2, #57).
- **Таблица `device_connections`** (состояние WS-соединения) (P3, #58).
- **Эмулятор WS-стека** — пакеты `scud_protocol` / `scud_server` /
  `scud_emulator`, WS-команды CLI и WS-тесты; эмулятор оставлен как
  MQTT-relay-двойник (`scud relay run`) (P4, #59).
- Документация WS-протокола `openapi_device.md`; `ws:` / `wss:` в CSP
  `connect-src` gateway; nginx WS-upgrade и WS-troubleshooting в DEPLOYMENT (P6).

### Changed

- **Транспорт по умолчанию при создании шкафа** — `mqtt`; `relay_id` обязателен.
  Создание шкафа с `transport=ws` отклоняется (400) (P3, #58).
- **`CommandType.config`** выведен из эксплуатации: ни одна роль не вправе
  создать config-команду (403); значение остаётся валидным в исторических
  записях (P3, #58).
- Документация (Architecture / Technologies / CONVENTIONS / DEPLOYMENT / gateway /
  openapi) обновлена под MQTT-only транспорт устройств (P6).

## 2026-06-26 — Outbox: наблюдаемость + устойчивость доставки в history_api

Очередь `outbound_history_events` (телеметрия/медиа/события → history_api)
получила мониторинг и защиту от класса отказа, который ронял dispatcher:
накопившийся хвост крупных base64-media событий грузился в память одним
батчем и выжигал OOM, а недоставленные события копились бесконечно. Branch
`outbox-observability-resilience`.

### Added

- **feat(monitoring):** дашборд **SCUD Outbox** (`provision_dashboards.py`) +
  5 dbprobe-метрик на `postgres_locker` (`outbox_pending`, `outbox_oldest_age_s`,
  `outbox_pending_bytes`, `outbox_retrying`, `outbox_deadletter`) в `dbprobe.sh`
  и шаблоне `scud-infra-postgres` + 4 триггера: бэклог растёт, доставка встала,
  pending-байты высоки (OOM-предвестник), есть dead-letter. Пороги — макросы
  `{$OUTBOX.PENDING.HIGH}`/`{$OUTBOX.OLDEST.MAX.S}`/`{$OUTBOX.BYTES.HIGH}`.
- **feat(locker_api):** dead-letter — `outbound_history_events.dead_lettered_at`;
  dispatcher паркует событие после `OUTBOUND_DISPATCHER_MAX_ATTEMPTS` (default 20)
  неуспешных доставок вместо бесконечного ретрая; запаркованные не выбираются
  `fetch_pending`. Метрика `scud_outbound_dead_lettered_total{channel}`.
- **feat(locker_api):** `outbound_history_events.locker_id` (FK
  `lockers ON DELETE CASCADE`) — проставляется из payload при enqueue. ФИЗИЧЕСКОЕ
  удаление локера автоматически уносит его недоставленные события (раньше
  admin-hard-delete оставлял мусорные строки). Soft-delete (`archived`) события
  не трогает.

### Changed

- **perf(locker_api):** dispatcher доставляет батч с ограниченной конкуренцией
  (`OUTBOUND_DISPATCHER_CONCURRENCY`, default 8) вместо последовательной
  доставки (~2/с не разгребала бэклог), а `fetch_pending` ограничивает батч по
  суммарному `pg_column_size(payload)` (`OUTBOUND_DISPATCHER_MAX_BATCH_BYTES`,
  default 32 МиБ) — крупные media больше не грузятся как сотни МБ за раз.

## 2026-06-25 — Наблюдаемость: детальный мониторинг gateway (Apache mod_status)

Дашборд **SCUD Gateway** и метрики воркеров/throughput шлюза. Branch
`monitoring-host-container-metrics`.

### Added

- **feat(gateway):** внутренний (порт 81) endpoint `mod_status` —
  `backend/gateway/status.conf` поднимает отдельный `VirtualHost *:81` с
  default-deny и единственным `SetHandler server-status` (`LocationMatch
  ^/server-status$`). Порт 81 **не публикуется** в `docker-compose.yaml`
  (достижим только внутри `backend_net`, как `/metrics` бэкендов); публичный
  `:80` статистику не отдаёт. Dockerfile: `a2enmod status` + удаление
  дистрибутивного глобального `status.conf`.
- **feat(gateway):** `backend/gateway/mpm.conf` — `ServerLimit ==
  MaxRequestWorkers` (150). Длина scoreboard в `server-status?auto` =
  `ServerLimit`; без закрепления она могла бы разойтись с `MaxRequestWorkers`,
  и `worker_util_pct` (знаменатель = slots_total) никогда не достиг бы 100 %, а
  триггеры насыщения не сработали бы. Ёмкость шлюза не меняется (150 → 150).
- **feat(monitoring):** расширен шаблон `scud-infra-gateway` — HTTP-агент master
  `scud.gateway.status.raw` (скрейпит `:81/server-status?auto`) + 21 dependent-item
  (`busy_workers`/`idle_workers`/`slots_total`/`worker_util_pct`, разбор scoreboard,
  `req_rate`/`bytes_per_sec`/`duration_per_req`/`cpu_load`/`load1`/`uptime`) с
  REGEX/JAVASCRIPT-препроцессингом (on-fail → `0`). 3 новых триггера: насыщение
  пула воркеров (> `{$GATEWAY.WORKERS.HIGH.PCT}` = 85 %), исчерпание (> 95 %),
  недоступность server-status (nodata `:81`).
- **feat(monitoring):** дашборд **SCUD Gateway** в `provision_dashboards.py`
  (страницы «Workers & saturation» / «Scoreboard» / «Throughput & process»).
  MPM шлюза — prefork: `BusyWorkers` у предела `MaxRequestWorkers` (150) =
  потолок одновременных соединений, включая WebSocket-туннели устройств.

### Changed

- **test(monitoring):** +тесты шаблона/триггеров/дашборда + security-regression
  guard (`test_gateway_status_endpoint.py`: server-status не на `:80`, порт 81 не
  публикуется — парсит `ports` сервиса, а не строку; `ServerLimit ==
  MaxRequestWorkers`).
- **fix(monitoring):** float-регексы gateway (`ReqPerSec`/`BytesPerSec`/
  `BytesPerReq`/`DurationPerReq`/`CPULoad`/`Load1`) принимают экспоненциальную
  запись `%g` (`([0-9.]+(?:[eE][+-]?[0-9]+)?)`) — иначе на очень больших/малых
  значениях `[0-9.]+` молча обрезало по `e` и сохраняло неверное число.
  `nodata`-триггер scrape-health переведён на dependent с историей
  (`busy_workers`), а не на master с `history:"0"`. Lifetime-средние
  (`DurationPerReq` и др.) помечены как искажённые WS-туннелями.

## 2026-06-23 — Наблюдаемость: алертинг, SLO, syslog-ng, сигналы сбоев (Этап 3, без OpenTelemetry)

Закрытие хвостов дорожной карты наблюдаемости (`docs/review/observability-2026-06-19.md`
§5, пункты #2/#12/#13/#14/#15). Вне объёма: #16 OpenTelemetry и развёртывание стека
на стенде. Branch `observability-etap3-gaps`.

### Added

- **feat(metrics):** `scud_unhandled_errors_total{service}` — счётчик действительно
  необработанных исключений 500-класса (QueryError / ValueError / неклассифицированные),
  доходящих до центрального `write_error`; **не** инкрементируется на ожидаемых
  `UpstreamUnavailableError` (503) и `AsyncPostgresError`. Never-raises, в обоих
  `ProblemDetailsHandler.write_error` и `JSONHandler.write_error`.
- **feat(metrics):** `scud_partition_create_failures_total{service,table}` — сигнал о сбое
  создания месячной партиции в `history_api` (ранее только лог, невидимый для Zabbix);
  инкремент в `retention._create_future_partitions`.
- **feat(monitoring):** сертифицированный сайдкар **syslog-ng** (Astra `main`,
  `monitoring/syslog-ng/`) в профиле `monitoring` — централизованная агрегация JSON-логов
  всех сервисов через tail json-file логов в named volume `syslog_ng_logs`; драйвер
  `json-file` не меняется (`docker compose logs` сохраняется), приложения не трогаются.
- **feat(monitoring):** доставка алертов в **Max** — `alert-delivery.yaml` (WEBHOOK
  media type) + идемпотентный `provision_alert_routing()` (usergroup «SCUD On-Call» +
  on-call пользователь + action на severity ≥ HIGH с recovery) + глобальный макрос
  `{$MAX.BOT.TOKEN}` из env (best-effort; токен никогда не логируется).
- **feat(monitoring):** **SLI/SLO** в `template-scud-services.yaml` — 12 CALCULATED-items
  (error-ratio 5m/1h + burn-rate fast/slow) для трёх SLO: доступность логина 99.5%,
  P99 латентности команды ≤ 5 с при 99%, доставка OTP 99%; + 6 multi-window burn-rate
  триггеров (fast-burn HIGH > 14.4×, slow-burn AVERAGE > 1×, с порогом минимального трафика).
- **feat(observability):** сигнал сбоя бэкапа — `backup.sh` пишет
  `last_run_status` (`OK`/`FAIL <dbs>`), zabbix-agent2 UserParameter `scud.backup.status`
  + item/триггеры в `template-scud-infra.yaml` (HIGH на `FAIL`, AVERAGE на простой > 25 ч).

### Changed

- **docs(observability):** все фантомные комментарии про Sentry (SDK нигде не подключён)
  заменены на правдивое описание реального пути алертинга (структурный лог + Zabbix-триггер).
- **ci:** hadolint и shellcheck расширены на `monitoring/syslog-ng/` и
  `backend/scripts/tests/*.sh`.

### Verification

- **live-import** `provision.py` против реального Zabbix 7.0.10 — CLEAN + IDEMPOTENT для
  `template-scud-services.yaml` и `template-scud-infra.yaml` (подтверждены синтаксис
  CALCULATED-items и `mediaTypes`-only импорт media-типов; usergroup/user/action создаются
  через API). Полный локальный CI-зеркал (8 линтеров + mypy×8 + 8 наборов тестов + gateway
  + monitoring, `down -v` между) — зелёный.

## 2026-06-23 — Мониторинг: доменные метрики locker_api в Zabbix (Этап 1, gap-closure)

### Added

- **feat(metrics):** новые Prometheus-метрики в `shared.metrics`, продьюсируемые
  `locker_api`:
  - `scud_command_pending_total` — gauge глубины backlog-очереди команд (незаквитированные
    команды, ожидающие доставки на устройства);
  - `scud_ws_auth_success_total` — счётчик успешных Ed25519-аутентификаций WS-устройств;
  - `scud_ws_events_total{event}` — счётчик lifecycle-событий WS-соединения
    (`heartbeat_timeout`, `supersession`, `close_normal`);
  - `scud_ws_telemetry_dropped_total` — кадры телеметрии, отброшенные из-за
    режима обслуживания или невалидной полезной нагрузки;
  - `scud_interservice_request_duration_seconds` — histogram латентности исходящих
    HTTP-запросов между сервисами (labels: `service`, `target`, `method`, `status_class`);
  - `scud_mqtt_relay_messages_total{outcome}` — счётчик relay-сообщений MQTT
    по исходу (`delivered`, `dropped_no_connection`, `dropped_queue_full`, `handler_error`);
  - `scud_telemetry_coerce_total{severity}` — коэрция значений телеметрии:
    инкрементируется только при реальных коэрциях чужого типа (`COERCED`/`NORMALIZED`),
    без спама на lossless `int→float`.
- **feat(monitoring):** расширение `monitoring/zabbix/template-scud-services.yaml`
  (commit `e2a4693`) — 9 дополнительных зависимых items для `locker_api`:
  `scud_command_pending_total`, `scud_command_lifecycle_failed`, `scud_ws_auth_failures_total`,
  `scud_ws_auth_success_total`, `scud_ws_events_heartbeat_timeout`,
  `scud_ws_events_supersession`, `scud_ws_telemetry_dropped_total`,
  `scud_mqtt_relay_messages_dropped`, `scud_telemetry_coerce_total`.
  Плюс `scud_interservice_errors` (фильтр `outcome=~"error|exhausted"` по
  `scud_interservice_requests_total`) на всех 7 сервисах.
- **feat(monitoring):** 18 новых триггеров в шаблоне (итого 60):
  - locker_api: `command failure rate > 5` (5 m), `WS auth brute force > 100` (5 m),
    `relay drop > 20` (5 m), `command backlog > 50` (10 m);
  - все 7 сервисов: `interservice error rate > 5` (5 m);
  - все 7 сервисов: `DB error rate > 5` (5 m).
- **verification:** live-import `provision.py` против Zabbix 7.0.10 — CLEAN (без API-ошибок),
  повторный запуск — IDEMPOTENT (2026-06-23, branch `observability-etap1-gaps`).

**Примечание:** `scud_interservice_request_duration_seconds` (histogram) экспортируется на
`/metrics`, но в Zabbix-шаблоне не представлен отдельным item — `histogram_quantile`
несводим к одному JSONPath-выражению зависимого item в Zabbix 7.0. Алертинг по
`scud_interservice_errors` покрывает действенный сигнал сбоев.

## 2026-06-21 — Мониторинг: инфра-шаблон Zabbix и agent2 (Этап 2)

### Added

- **feat(monitoring):** инфра-шаблон Zabbix `monitoring/zabbix/template-scud-infra.yaml`
  — LLD-обнаружение 6 PgBouncer- и 6 PostgreSQL-инстансов через UserParameter
  `scud.dbprobe[pgb|pg,<host>,<metric>]` → `dbprobe.sh` (`psql`); ключевые метрики:
  `cl_waiting`, `sv_active`, `maxwait`, `avg_query_time` (pgbouncer) и
  `numbackends`, `deadlocks`, `xact_rollback`, `longest_txn_seconds`,
  `history_partition_bytes` (postgres). HTTP-item `scud.infra.redis.up` — gauge
  достижимости Redis (fallback: встроенный redis-плагин agent2 отсутствует в
  Astra-сборке 7.0.10, подробности в `monitoring/CERT_GATE.md` §A2-2).
- **feat(monitoring):** контейнер `zabbix_agent2` в профиле `monitoring` —
  сертифицированный пакет `zabbix-agent2 1:7.0.10` из Astra `main`, непривилегированный,
  active-mode (`ServerActive=zabbix_server:10051`), без публикуемых портов.
  Монтирует `pgb_admin_password` и 6 `postgres_<svc>_password`; выполняет `dbprobe.sh`
  через `postgresql-client-15` (тоже из `main`). Redis-credential не монтируется.
- **feat(locker_api):** метрики MQTT-брокера — `locker_api` подписывается на
  `$SYS/broker/#` и зеркалит показания в Prometheus-gauges:
  `scud_mqtt_connected`, `scud_mqtt_relays_online`,
  `scud_mqtt_broker_messages_received_total`, `scud_mqtt_broker_messages_sent_total`,
  `scud_mqtt_broker_bytes_received_total`, `scud_mqtt_broker_bytes_sent_total`.
  Scrape в services-шаблоне Zabbix — без отдельного infra-item.
- **feat(monitoring):** `pgb_admin_password` повышен до Docker secret (ранее —
  захардкоженный дефолт в `pgbouncer/entrypoint.sh`). Читается и пулером (для
  генерации `userlist.txt`), и `zabbix_agent2` (для `SHOW STATS`/`SHOW POOLS`).
  Dev-значение сидируется `scripts/seed-dev-secrets.sh`, production — `openssl rand -hex 16`.
- **feat(services):** все 7 backend-сервисов экспортируют эндпоинты глубокого
  healthcheck: `/live` (liveness, без зависимостей), `/ready` (критические + мягкие
  зависимости → 200 ok / 200 degraded / 503 unavailable), `/health` (DB-only,
  остаётся для docker healthcheck). Матрица мягких зависимостей:
  Redis у `user_api`/`acm`; MQTT у `locker_api`.

## 2026-06-20 — Мониторинг: Zabbix consumer-стек и документация наблюдаемости

### Added

- **feat(monitoring):** Zabbix 7.0.10 consumer-стек (`monitoring` compose profile) —
  `postgres_zabbix` + `zabbix_server` + `zabbix_web` в `backend_net`. Шаблон
  `monitoring/zabbix/template-scud-services.yaml` покрывает все 7 backend-сервисов:
  HTTP-agent master item → `PROMETHEUS_TO_JSON` → LLD rules для авто-обнаружения серий
  → зависимые items для ключевых метрик (requests, latency, pool saturation, commands,
  security events, login attempts). 42 baseline-триггера (6 на сервис).
- **feat(monitoring):** идемпотентный stdlib-provisioner `monitoring/zabbix/provision.py`
  импортирует шаблон через Zabbix JSON-RPC API (`createMissing+updateExisting`);
  повторный запуск безопасен. Без сторонних зависимостей.
- **docs(monitoring):** раздел «Monitoring (Zabbix consumer-стек)» в
  `backend/DEPLOYMENT.md` — подъём профиля, генерация `postgres_zabbix_password`,
  форматы логов (`LOG_FORMAT=json/text` через `shared.observability.configure_logging`,
  `X-Request-ID`-корреляция), справочник новых метрик Этап-0.
- **docs(monitoring):** `monitoring/zabbix/README.md` — пошаговый ранбук: автоимпорт
  через `provision.py`, ручной fallback через UI (Data collection → Templates → Import),
  переменные среды, что опрашивает шаблон.

## 2026-06-20 — Наблюдаемость: метрика событий физической безопасности и SIEM-логи

### Added

- **feat(metrics):** счётчик `scud_security_events_total{service,type,severity}` в
  `shared.metrics`. Инкрементируется в продьюсерах `locker_api` (`security_event_producer.py`,
  `offline_producer.py`) в момент постановки события в outbound-очередь — ещё до доставки в
  `history_api`. Кардинальность ограничена: `type` ∈ `SecurityEventType` (8 значений),
  `severity` ∈ `EventSeverity` (≤ 5), итого ≤ 40 активных серий на сервис.
- **feat(observability):** структурированные SIEM-логи в двух точках:
  - `locker_api / state_monitor` — при переводе шкафа в `alarm` фиксируется строка уровня
    `WARNING` (или `CRITICAL` для `tamper`) с полями `event=security_alarm`, `type`,
    `severity`, `locker_id`, `sensor_id`. Без PII.
  - `history_api / InternalSecurityEventHandler` — при успешном персисте события фиксируется
    строка уровня `INFO` с полями `event=security_event_persisted`, `id`, `type`, `severity`,
    `locker_id`, `sensor_id`, `occurred_at`. Единственная точка с присвоенным `id` и
    временной меткой из БД.

### Tests

- **test(shared):** `shared/tests/unit/test_security_events_metric.py` — регистрация и
  инкремент `scud_security_events_total` через `shared.metrics`.
- **test(locker_api):** `tests/unit/test_security_event_producers.py` — `publish()` инкрементит
  счётчик по `{type, severity}`; `tests/unit/test_offline_producer.py` — `publish_offline()`
  инкрементит `{offline, medium}`.
- **test(locker_api):** `tests/integration/test_security_events_metric_endpoint.py` — реальный
  sweep `StateMonitor` инкрементирует серию; эндпойнт `/metrics` отдаёт корректное значение.

## 2026-06-17 — Наблюдаемость: насыщение пула соединений БД в Prometheus

### Added

- **feat(metrics):** четыре gauge'а `scud_db_pool_connections_{in_use,idle,waiters,max}`
  с label'ами `{service, pool}` (label `pool` = имя БД). Семплируются pull-based
  на каждый scrape `/metrics` через `update_pool_gauges()` в `MetricsHandler` —
  нулевой оверхед на hot-path acquire/release.
- **feat(shared.metrics):** `register_pool(name, info_provider)` и
  `update_pool_gauges()` + реестр провайдеров пулов. `update_pool_gauges()`
  никогда не кидает (зеркалит `_record_db_metric`): закрытый пул и
  упавший провайдер пропускаются без падения scrape.
- **feat(shared.database):** публичное свойство `AsyncPostgres.pool_name`
  (имя БД для label `pool`, без обращения к приватному `_config`).

### Changed

- **AsyncPostgres.get_pool_info()** дополнен ключами `in_use`/`idle`/`waiters`/
  `database` (in_use из `_pool._used`, idle из `_pool._pool`); историческое
  поле `pool_size` сохранено.
- **AsyncPostgres._acquire_and_run** инкрементит/декрементит счётчик
  `_pool_waiters` вокруг `await asyncio.sleep` в re-poll-ветке исчерпания пула;
  декремент в `finally` исключает утечку счётчика при `CancelledError`.
- Все 7 backend-сервисов вызывают `register_pool(db.pool_name, db.get_pool_info)`
  после инициализации пула.

### Tests

- **test(locker_api):** `tests/unit/test_pool_metrics.py` — `update_pool_gauges()`
  проставляет gauge'и, пропускает закрытый пул, глотает исключение провайдера;
  `get_pool_info()` рапортует saturation; `_acquire_and_run` не теряет счётчик
  ожидающих.
- **test(locker_api):** `tests/integration/test_pool_metrics_endpoint.py` —
  `/metrics` экспортирует серии пула с `service="locker_api"`, `pool="scud_lockers"`.

## 2026-06-14 — notification_api: OTP не хранится после отправки (SEC-18)

### Changed
- `notification_api`: `mark_sent`/`mark_failed` обнуляют `outbound_max_messages.body`
  (код второго фактора) в той же `UPDATE`; колонка `body` стала nullable.
  Планировщик удаляет просроченные записи и терминальные (`sent`/`failed`)
  старше `TERMINAL_RETENTION_SECONDS` (24 ч).

### Обновление существующих установок
Блок `outbound-max-body-nullable` (`scud_notifications`) применяет
`deploy.sh update`; вручную — `backend/DEPLOYMENT.md` §6. Без него
`mark_sent`/`mark_failed` падают с `NotNullViolation`.

## 2026-06-12 — Эмулятор: авто-пороги датчиков при провижининге

### Added

- **feat(emulator):** `scud_emulator.thresholds` — вывод дефолтного порога из
  (тип датчика, генератор): bool-тревожные (intrusion/tamper/shock/door) →
  `fixed_value="false"`, numeric → `min`/`max` из границ генератора
  (`scenario` → baseline); `constant`, `composite` и неизвестные типы — без порога.
- **feat(emulator):** `provision_locker` после создания датчика создаёт порог через
  `POST /api/v1/locker/{id}/sensors/{sid}/thresholds`; ошибки (HTTP-статус и
  сетевые) — warning, не фатальны. Раньше свежепровиженный флот не генерировал
  security-событий: StateMonitor видит датчик только через INNER JOIN с
  `sensor_thresholds` (симптом «фото есть, событий нет», диагноз стенда 2026-06-10).

### Tests

- **test(emulator):** `tests/test_emulator/test_thresholds.py` — матрица вывода
  порогов (15 тестов), включая приоритет типа датчика над генератором и
  composite-кейсы.
- **test(emulator):** `tests/test_emulator/test_provisioning.py` — пороги
  создаются для выводимых датчиков, пропускаются для constant/уже
  зарегистрированных; HTTP 500 и transport-ошибки на пороге не фатальны.

## 2026-06-11 — Аудит стенда: ACM-матрица, коэрция телеметрии, ротация логов mqtt

### Fixed

- **fix(acm):** закрыты пропуски ACM-матрицы — эндпоинты, отдававшие 403 всем
  ролям из-за default-deny: `PATCH /api/v1/events/security/{id}` (закрытие
  события из админки, OPERATOR+), `POST /api/v1/events/security|/telemetry|
  /commands/history` (внешний ingest, ADMINISTRATOR), `GET /api/v1/contractor/{id}`
  (OPERATOR+), singular `/api/v1/emergency-request/{id}` (GET — WORKER,
  DELETE и `/logs` — OPERATOR+), `PUT/DELETE /api/v1/sensor-types/{id}`
  (ADMINISTRATOR).
- **fix(acm):** `DELETE /api/v1/auth/users/{id}/tokens` перехватывался публичным
  правилом `^/api/v1/auth/` (gateway не передавал identity-заголовки → всегда 401).
  Вся ветка `/auth/users/` теперь требует ADMINISTRATOR и стоит до публичного правила.
- **fix(acm):** устранено затенение first-match в секции «Шкафы»: специфичные
  правила sensors/thresholds подняты выше широких `^/api/v1/locker/`. Фактические
  роли восстановлены по записанному intent: чтение/удаление сенсоров — OPERATOR
  (было WORKER/ADMINISTRATOR через широкие правила), создание/изменение/удаление
  порогов — ADMINISTRATOR (было OPERATOR).
- **fix(acm):** locker-scoped проверка Допуска нормализует регистр UUID — путь
  с uppercase-UUID больше не обходит проверку (паттерн матчился только на lowercase
  и проверка молча пропускалась).
- **fix(gateway):** добавлен ProxyPass для singular `/api/v1/emergency-request/`
  (GET/DELETE заявки, `/logs`) — раньше запросы проваливались в SPA DocumentRoot.
- **fix(locker_api):** коэрция значений телеметрии переписана как декларативная
  матрица с серьёзностью (CANONICAL / NORMALIZED / COERCED): lossless `int→float`
  для numeric-датчиков больше не пишет WARNING на каждое целое значение
  (источник спама — клампы генераторов эмулятора), метрика и WARN остались
  только для настоящих коэрций чужого типа.

### Ops

- **ops(compose):** сервису `mqtt` добавлен `logging: *default-logging` —
  единственный production-сервис без rotation-якоря; NOTICE-шум TCP-healthcheck
  (2 строки / 10 с) растил json-file лог без ограничения.
- **ops(compose):** dev-дефолт `LOGIN_RATE_LIMIT` поднят 30 → 100 (по образцу
  `/verify`): gateway_tests создают class-scoped сессии с одного IP и упирались
  в лимит. Production снижает через `.env` (чеклист в `services/auth_api/README.md`).

### Tests

- **test(gateway):** end-to-end allow+deny пары через Apache+Lua+ACM для всех
  исправленных классов эндпоинтов (95 тестов в gateway-сьюте).
- **test(acm):** регрессионный класс `TestStandAuditRegressions` + расширенная
  матрица `_FIND_RULE_CASES` (115 unit-тестов).

## 2026-06-01 — Tamper media (фото и видео при взломе)

- **feat:** При срабатывании датчика `intrusion`/`tamper` шкаф передаёт прикреплённые
  фото и видео через chunked WebSocket-протокол (`media_upload_begin` / `media_chunk` /
  `media_upload_end`, sha256-валидация, до 10 МБ на трансфер).
- **feat:** REST для оператора (роль OPERATOR+):
  `GET /api/v1/media`, `GET /api/v1/media/{id}` (отдаёт байты с `Content-Type`),
  `GET /api/v1/events/security/{id}/media`.
- **feat:** Эмулятор: команда `scud locker tamper <local-id>` + REST
  `POST /api/v1/locker/{id}/tamper` — переключает intrusion-сенсор и шлёт
  встроенные минимально валидные JPEG (1×1) + MP4-stub.
- **feat:** `MediaRepository.find_event_in_window(±30s)` связывает медиа со
  security_event по `sensor_id`+`occurred_at`, когда `security_event_id` не передан.
- **breaking:** Таблица `locker_photos` переименована в `locker_media`, поля расширены
  (`kind` ENUM('photo','video'), `security_event_id`, `bytes BYTEA` для inline-хранения,
  `duration_ms` для видео). Старые endpoints `/api/v1/photos` и `/internal/api/v1/photos`
  удалены — клиенты переводятся на `/api/v1/media`.
- **breaking:** WS-сообщение `photo_upload` удалено в коде (помечено `deprecated`
  в `openapi_device.md`). Эмулятор и устройства должны использовать
  `media_upload_begin/chunk/end`.
- **ops:** Apache gateway теперь проксирует `/api/v1/media` (см. `backend/gateway/apache.conf`).
- **ops:** `MediaProducer` использует 15 с initial dispatch delay в outbound-очереди,
  чтобы дать `state_monitor` записать security_event раньше, чем медиа попадёт в
  history_api.

## [Unreleased] — 2026-05-29 (Step-up 2FA для команд шкафа)

### Added

- **feat(auth_api):** новые internal-эндпоинты `/internal/api/v1/step-up/issue`
  и `/internal/api/v1/step-up/verify`, таблица `step_up_challenges` в
  `scud_auth` (TTL 5 мин, lockout по `OTP_MAX_ATTEMPTS`). Cleanup истёкших
  challenges — раз в час в общем `cleanup_loop`.
- **feat(locker_api):** `POST /api/v1/locker/{id}/commands/confirm` — второй
  шаг flow с OTP-кодом. Обычный `POST /commands` теперь может вернуть `202` +
  `challenge_id` для пользователей с `require_2fa=true` на привилегированных
  командах (`open` / `reboot` / `config`). Команды `close` и любые команды от
  пользователей без 2FA — без изменений (один шаг, `201`).
- **feat(acm,gateway):** ACM пробрасывает `X-Remote-Require-2FA: true` через
  ответ `/acm/check`, Apache Lua форвардит в downstream-сервисы.
- **feat(history):** новые security events `command_2fa_issued` (info),
  `command_2fa_verified` (info), `command_2fa_failed` (warning),
  `command_2fa_mismatch` (alert) для аудита привилегированных операций.
  Prometheus-счётчики в `shared.metrics`.

### Ops

- Новый секрет `INTERNAL_STEP_UP_HMAC_KEY` (общий для auth_api / user_api /
  locker_api). Dev-default добавлен в `scripts/seed-dev-secrets.sh`.
  Production-runbook (`openssl rand -hex 32` + синхронная ротация трёх
  сервисов) — `backend/DEPLOYMENT.md` § Secret rotation.
- `locker_api` теперь зависит от `auth_api` функционально (для step-up issue/verify),
  но без hard `depends_on` — на окне недоступности auth_api сервис вернёт 503
  для привилегированных команд, остальные потоки работают.

## [Unreleased] — 2026-05-26 (Reports module)

### Added

- feat(reports): отчёт locker_open теперь выгружается как оформленный .xlsx
  (заголовок с периодом, цветная шапка, рамки, «зебра», авто-ширина, закреплённая
  шапка, автофильтр) — через собственный stdlib-writer без новых зависимостей.
- test(system): опциональное сохранение артефактов теста (SAVE_ARTIFACTS) для
  ручного просмотра сгенерированных отчётов.
- **Новый сервис `reports_api`** (порт 8006, БД `scud_reports`) —
  асинхронная генерация отчётов СКУД. Схема «поставил задачу → опросил
  статус → скачал файл»: `POST /api/v1/reports` ставит задачу в очередь
  и возвращает `202`, фоновый воркер генерирует файл, клиент скачивает его
  через `GET /api/v1/report/{id}/download`.
- **Отчёт «по открытию» (`locker_open`)** в `.xlsx` за произвольный период
  (`date_from`/`date_to`). Скачивание по ссылке с TTL (по умолчанию 24 ч);
  после истечения файл удаляется фоновым воркером, скачивание отдаёт `410`.
- **`history_api`: внутренний эндпоинт** `GET /internal/api/v1/commands/history`
  — обобщённая выборка истории команд с фильтрами `command_type`/`status`,
  диапазоном по `executed_at` и порядком (`created_at_desc`/`executed_at_asc`),
  источник данных для `reports_api` (только из Docker-сети).

### Ops

- Новый volume `reports_data` для сгенерированных файлов (regenerable,
  backup опционален) и БД `postgres_reports`/`scud_reports`.
- Новые env: `REPORTS_STORAGE_PATH` / `REPORT_LINK_TTL_SECONDS` /
  `REPORT_WORKER_INTERVAL_S` / `REPORT_MAX_ATTEMPTS` / `HISTORY_API_URL`.
  Отдельный секрет не требуется — используется общий `postgres_password`.

## [Unreleased] — 2026-05-24 (Max messenger 2FA)

### Added

- **Доставка OTP-кодов второго фактора через бота мессенджера Max**
  (`https://platform-api.max.ru`). `auth_api` больше не ходит во внешний
  SMS-провайдер — `LoginHandler` через `NotificationClient` бьёт во
  `POST /internal/api/v1/otp/send` в `notification_api`, который
  отправляет код через `MaxBotClient.send_text(max_user_id, ...)`.
- **Привязка Max-бота к пользователю.** При создании пользователя
  `POST /api/v1/users` возвращает одноразовый `max_invite_token` и
  готовый deep-link `https://max.ru/<bot_username>?start=<token>`.
  Пользователь жмёт «Начать» в Max-боте; `MaxBotPoller` в `notification_api`
  ловит событие `bot_started`, идёт во `user_api`
  `POST /internal/user/by-invite` (HMAC) — там атомарно проставляется
  `users.max_user_id` и помечается токен `used_at=now()`. После привязки
  накопленные коды из `outbound_max_messages` доставляются «лежащему»
  пользователю.
- **Новый ProblemType `max-binding-required` (401).** Если у пользователя
  верный пароль, но `users.max_user_id` пуст — `/auth/login` отвечает
  RFC 7807 с `type=https://api.scuddocs.com/problems/max-binding-required`
  и подсказкой обратиться к админу за invite-ссылкой.
- **БД-схема:** поле `users.max_user_id BIGINT NULL` + partial UNIQUE
  индекс; таблицы `max_invite_tokens` (TTL 24h), `outbound_max_messages`
  (очередь исходящих с tracking sent_at/failed_at/attempts) и single-row
  `bot_poller_state` для маркера long-polling-курсора.
- **HMAC-разделение каналов.** Введены отдельные ключи
  `INTERNAL_OTP_HMAC_KEY` и `INTERNAL_MAX_BINDING_HMAC_KEY` — компрометация
  одного не открывает доступа к другому. Формат подписи —
  `timestamp + sha256(body)`, общий helper
  `shared.utils.compute_request_signature`/`verify_request_signature`.

### Changed

- `/internal/auth/check` теперь возвращает дополнительное поле
  `max_user_id: int | None` — `auth_api` знает привязку из одного запроса.
- `shared.http.problem`: добавлены `ProblemType.MAX_BINDING_REQUIRED` и
  `ProblemTitle.MAX_BINDING_REQUIRED`.

### Removed (BREAKING)

- **SMS-канал OTP в `auth_api` удалён полностью.** Из кода ушли
  `_send_sms`, `_warn_if_sms_disabled`, settings `sms_client`/
  `sms_provider_url`, env `SMS_PROVIDER_URL`. В production-сборке
  `auth_api` больше не зависит от внешнего SMS-провайдера. Notification
  API сохраняет stub-логику `_send_sms` для периодических/разовых
  notification-flow — это не OTP-канал.

### Ops

- Новые секреты в `backend/secrets/`: `max_bot_token`, `internal_otp_hmac_key`,
  `internal_max_binding_hmac_key`. Дев-defaults обновлены в
  `scripts/seed-dev-secrets.sh`. Production-runbook ротации — в
  `backend/DEPLOYMENT.md` § Secret rotation.
- Новые env-переменные: `MAX_BOT_API_URL` (default
  `https://platform-api.max.ru`), `MAX_BOT_USERNAME`, `NOTIFICATION_API_URL`.

## [Unreleased] — 2026-05-19 (lint + coverage: multi-stage Dockerfile, CI job lint)

### Added

- **CI job `lint`** (`.github/workflows/tests.yml`) — параллельно с
  `backend-tests`. Запускает:
  - **ruff** (max ruleset: E/W/F/B/C4/UP/SIM/S/RET/PL/RUF/I/N/T20/LOG/PIE/ERA/ASYNC/ARG/PT/RSE/TCH) на `backend/services` + `backend/shared`;
  - **mypy --strict** с baseline-overrides для `dacite`/`psycopg2`/`tornado`/`shared.database`;
  - **bandit** на `backend/services` + `backend/shared`;
  - **sqlfluff** (Postgres dialect) на миграциях;
  - **hadolint** на всех Dockerfile;
  - **shellcheck** на `backend/scripts/*.sh`;
  - **luacheck** на `backend/gateway/lua/`.
  Линтеры ставятся в раннере через pip/apt/curl — НИКОГДА не попадают в
  Astra-сертифицированную production-сборку. Конфиги: корневой
  `pyproject.toml`, `.hadolint.yaml`, `.luacheckrc`, `.shellcheckrc`.
- **Coverage** — был заблокирован отсутствием `python3-coverage` в Astra
  `main`. Решение: multi-stage Dockerfile. Каждый сервисный Dockerfile
  получил `FROM ... AS production` (текущий контент, certified-only) +
  `FROM production AS test`, который временно подключает Astra extended
  через `/etc/apt/sources.list.d/extended.list`, ставит `python3-coverage`
  и `python3-pytest-cov`, потом удаляет файл-список. Test stage никогда не
  публикуется как production: `docker compose build --target production` /
  `compose.build.target: production` физически собирает только первый
  слой. `*_tests` сервисы переведены на `target: test` + entrypoint
  `pytest --cov --cov-report= ...` с `COVERAGE_RCFILE` и `COVERAGE_FILE`
  через env. Per-service данные пишутся в bind-mounted
  `backend/.coverage-data/<svc>/` (gitignored), CI делает `coverage
  combine` дважды: per-matrix-service для артефакта и в отдельном
  `coverage-combine` job для суммарного отчёта (xml + HTML).
  `[tool.coverage.report] fail_under = 0` — baseline; поднимем после
  первого реального прогона.

### Changed

- **`CONVENTIONS.md`** дополнен правилами multi-stage Dockerfile и явного
  `build.target` в compose, secciya Linting добавлена; правило "Never use
  `pip install` in backend Dockerfiles" уточнено: запрет относится только
  к `AS production` stage, в `AS test` допускается extended `apt` (а pip —
  как последний резерв).
- **`.gitignore`** — `backend/.coverage-data/`, `.coverage`, `coverage.xml`, `htmlcov/`.

### Removed

- **TODO про coverage** из `.github/workflows/tests.yml` (строки 48-61
  до правки) — закрыт.

### Code modernization (через `ruff check --fix --unsafe-fixes`)

В рамках включения линтеров 179 файлов прошли auto-modernization:

- `Optional[X]` → `X | None` (PEP 604), 445 правок;
- `import` блоки пересортированы (isort);
- `datetime.timezone.utc` → `datetime.UTC` (Python 3.11+);
- `typing.Type[X]` → `type[X]`, `typing.List` → `list`, и т.п.;
- `isinstance(x, (A, B))` → `isinstance(x, A | B)`;
- удалены stale `# noqa: BLE001` (правило не включено в наш select).

Регрессий нет: `docker compose --profile tests run --rm user_api_tests`
прошёл 171/171 после правок. Изменения семантически идентичны (unsafe-fix
группа ruff покрывает только синтаксические трансформации).

### Baseline (важно!)

Первый прогон линтеров (локально, Docker `python:3.11-slim`):

- **bandit / sqlfluff / hadolint / shellcheck / luacheck** — зелёные после
  подбора обоснованного ignore (детали в самих конфигах). Все skips
  — false-positives с явными причинами в комментариях файлов конфигов.
- **ruff** и **mypy** — **БУДУТ КРАСНЫМИ в CI lint job** до follow-up PR.
  Это осознанный baseline-выбор: ignore-list содержит ТОЛЬКО обоснованные
  правила (cyrillic-look-alike, bandit-дубли, Tornado handler signatures),
  но НЕ скрывает реальные баги и серьёзную стилистику. Открытые категории
  (на момент мерджа): несколько closure-over-loop в тестах, `is None`
  literal comparisons, dangling asyncio.create_task, raise-without-from,
  ambiguous `l`-naming, ~500 mypy strict (Tornado-mixin attr-defined,
  untyped `__init__`, `dict`/`list` без параметров, `Locker | None`
  без None-check). Roadmap для mypy strict — в комментариях
  `pyproject.toml`. Точные счётчики в PR description, не здесь —
  устаревают по мере фиксов.

### Coverage (проверено)

`docker compose --profile tests run --rm user_api_tests` локально:

- 171 тест прошёл;
- pytest-cov собрал `.coverage.user_api.*` без warning'ов (через
  `backend/.coveragerc` — отдельный rcfile без `[paths]`, чтобы
  pytest-cov не применял mapping в cwd=/app/user_api контейнера);
- `coverage combine --rcfile=pyproject.toml` из корня репо нормализовал
  пути через per-service `[tool.coverage.paths]` группы (shared,
  user_api, locker_api, ...);
- итоговое покрытие user_api: **56%** (1903 statements, 762 missing).

В CI `coverage-combine` job соберёт все 6 сервисов. После первого
зелёного прогона поднимем `[tool.coverage.report] fail_under` в отдельном PR.

### Verification

Производственный образ должен быть байт-идентичен предыдущему (новый
`AS test` слой не используется при `--target production`). Проверка:
```bash
docker run --rm $(docker build -q --target production -t scud-auth-prod backend -f backend/services/auth_api/Dockerfile) \
    dpkg -l python3-coverage python3-pytest-cov 2>&1 | grep -E '(no packages|coverage)'
# Ожидается: "No packages found" — coverage отсутствует в production.
```

## [Unreleased] — 2026-05-19 (locker_api: who_open_last + operational_status в view=location)

### Added

- **`who_open_last` в ответе `GET /api/v1/locker/{id}`** — `Optional[int]`,
  ID пользователя, последним успешно открывшего шкаф. Заполняется по
  результату executed OPEN-команды устройством через WebSocket: в
  `app/websocket/device_handler.py:_handle_command_result` пробрасывается
  `command.created_by` (он же `X-Remote-User` инициатора команды) в
  новый kwarg `LockerRepository.update_door(..., who_open_last=...)`. Поле
  записывается одной транзакцией с `last_opened_at`, оба значения всегда
  согласованы. Семантика — **always-overwrite**: при system-issued OPEN
  (`created_by IS NULL`) предыдущее значение перезаписывается в NULL.
  Если потребуется сохранять «последнего известного» пользователя —
  замена SQL на `COALESCE(%s, who_open_last)` в `repositories/locker.py:update_door`.
- **`operational_status` в `LockerLocation`** — используется в
  `GET /api/v1/lockers?view=location` и
  `POST /api/v1/lockers/batch` с `view=location`. Фронтенд карты получает
  цвет маркера (normal/maintenance/alarm) без отдельного запроса на каждый
  шкаф. Колонка уже была в таблице `lockers` (`locker_op_status` enum) — SELECT
  в `repositories/locker.py:get_all` и `get_batch_location` теперь её
  выбирают; dacite-deserialization обоих call-сайтов переключён на общий
  `_DACITE_CONFIG` (он уже содержал cast `OperationalStatus`).

### Schema

- Колонка `who_open_last BIGINT NULL` вкомпилирована в `001_init.sql`
  (а не отдельной миграцией 002), так как развернутой инфраструктуры
  пока нет — все деплои свежие. Если/когда появятся существующие
  prod-БД, добавлять отдельной миграцией.

### Infrastructure

- **`.gitattributes` (новый)** — фиксирует `*.sh`/`*.lua`/`*.py`/`*.yaml`/
  `Dockerfile*` etc. на `text eol=lf`. Без него Windows-чекаут с
  `core.autocrlf=true` ставил CRLF в shell-скрипты, и postgres-init
  падал с `cannot execute: required file not found` при попытке запустить
  `init-locker-db.sh` (Linux-ядро искало интерпретатор `/bin/bash\r`).
- **`emulator/Dockerfile`** — apt-источники переведены на HTTPS-зеркало
  Yandex (`mirror.yandex.ru`), оригинальные debian-зеркала бывают
  недоступны из некоторых сетей. Эмулятор не входит в Astra-сертификацию,
  Debian-зеркало здесь допустимо.

### Fixed

- **Cross-locker command spoofing** (security, pre-existing — обостряется
  введением `who_open_last`): `CommandRepository.update_status_to_executed`
  и `update_status_to_failed` обновляли команду только по `id`, без
  проверки `locker_id`. Скомпрометированное устройство A могло прислать
  `command_result` с UUID команды устройства B и получить два эффекта:
  (1) пометка чужой команды как `executed`/`failed`, ломая её аудит и
  retry-логику; (2) после введения `who_open_last` — запись `created_by`
  чужой команды в поле `who_open_last` *своего* локера. Добавлен
  `AND locker_id = %s` в WHERE обоих UPDATE; `_handle_command_result` в
  `device_handler.py` пробрасывает `self.authenticated_locker.id` (доверенный
  идентификатор после Ed25519-аутентификации) вторым параметром в оба
  метода. При несовпадении locker_id `fetch_one` возвращает None — ветка
  обновления статуса и связанные эффекты пропускаются.
- **Cross-locker spoofing fix — дотяжка failure-ветки `_handle_command_result`.**
  В первой версии fix'а `update_status_to_failed` получил `locker_id` в WHERE,
  но дальше код делал `cmd_repo.get_by_id(command_id)` БЕЗ locker_id-фильтра
  и публиковал событие failed в `command_history`. Это позволяло устройству A
  создать ложное событие failed в истории локера B (DB-статус самой команды
  не менялся, но `command_history` засорялась чужими событиями). Failure-
  ветка переписана симметрично success: используется команда, возвращённая
  из `update_status_to_failed`; при None — `logger.warning` про возможный
  spoofing и `return` без публикации. Симметрично в success-ветке добавлен
  `logger.warning` + early return при None — SOC-аудит попыток.
- **`update_status_to_sent_batch` — defense-in-depth `AND locker_id = %s`.**
  Метод обновлял команды только по списку `id`. Текущий call-site в
  `_send_pending_commands` (`device_handler.py:870`) передаёт `sent_ids`,
  уже отфильтрованные через `get_pending_commands(self.authenticated_locker.id)`,
  но явный guard в WHERE защищает от случайной утечки в будущих call-site.
  Сигнатура: `update_status_to_sent_batch(command_ids, locker_id)`.

### Consistency

- `LockerRepository.get_batch_brief` (`repositories/locker.py:391`) теперь
  использует `_DACITE_CONFIG` для `from_dict` — синхронно с `get_batch`/
  `get_batch_location`. Текущий код работал, поскольку `LockerBrief` не
  содержит enum-полей, но добавление любого enum в будущем падало бы тихо
  на десериализации.

### Tests

- `tests/unit/test_locker_api.py` — добавлены тесты
  `test_get_locker_by_id_returns_who_open_last` и
  `test_get_lockers_view_location_includes_operational_status`; обновлены
  существующие view=location-тесты (новый набор ключей включает
  `operational_status`); поле `who_open_last` добавлено в shared `test_locker`
  fixture, иначе dacite ругается на отсутствие non-default поля.
- `tests/unit/test_locker_repository.py` — два новых класса:
  `TestUpdateDoorWhoOpenLast` (3 теста: OPEN с user_id, OPEN без user_id,
  CLOSED не трогает who_open_last) и `TestGetAllLocationView` (SELECT для
  `view=location` и `get_batch_location` содержит `operational_status`).
- `tests/unit/test_device_handler_command_result.py` — новый файл,
  3 теста на пробрасывание `command.created_by` в `update_door` (OPEN с
  пользователем, OPEN system-issued → None, CLOSE без kwarg).
- `tests/integration/test_who_open_last.py` — новый файл, 5 интеграционных
  тестов: null для свежего шкафа, заполнение после OPEN-команды,
  перезапись при последующих OPEN (включая system-issued), наличие
  `operational_status` в `view=location` и фильтр coherence
  (`?view=location&operational_status=maintenance`), `view=location` в
  batch-эндпоинте.
- `tests/integration/test_locker_integration.py` — два существующих
  view=location-теста обновлены (новый набор ключей); ручной UPDATE в
  `test_last_opened_at_set_on_door_open` теперь включает `who_open_last = NULL`
  для соответствия реальному SQL `update_door`.
- `tests/unit/test_command_repository.py` (новый, 4 теста) — защита
  cross-locker spoofing: `update_status_to_executed`/`_failed` принимают
  и проверяют `locker_id` в WHERE.
- `tests/unit/test_locker_repository.py::TestUpdateDoorNotFound` — 2 теста:
  `update_door` возвращает None при отсутствии локера (для OPEN и CLOSED
  веток), включая проброс нового kwarg `who_open_last`.
- `tests/unit/test_device_handler_command_result.py::TestHandleCommandResultLockerScope`
  — 4 теста: `update_status_to_failed` вызывается с `self.authenticated_locker.id`;
  failed-ветка с mismatched command_id НЕ вызывает `get_by_id` и НЕ публикует
  history; executed-ветка с mismatched command_id НЕ публикует history и НЕ
  трогает door; существующие 3 теста расширены проверкой
  `update_status_to_executed` с locker_id.
- `tests/integration/test_who_open_last.py::test_close_preserves_who_open_last`
  — регрессионная защита: после OPEN(42)→CLOSE поле `who_open_last == 42`
  и `last_opened_at` сохраняются.
- `tests/integration/test_command_repository_integration.py` (новый, real-DB)
  — 3 теста: cross-locker spoofing на `update_status_to_executed`,
  `update_status_to_failed` и `update_status_to_sent_batch` блокируется
  реальным PostgreSQL'ом. Текущие unit-тесты mock'ают `fetch_one=None`
  независимо от SQL и не поймали бы удаление `AND locker_id = %s` —
  этот integration тест ловит.
- `tests/unit/test_locker_repository.py::TestUpdateDoorLastOpenedAt::test_update_door_closed_sql_does_not_contain_who_open_last`
  — drift-guard: integration-helper `_set_door_closed` воспроизводит SQL
  CLOSED-ветки `update_door` (без `who_open_last`); этот unit-тест поймает,
  если кто-то когда-нибудь добавит `who_open_last = ...` в CLOSED SQL.

## [Unreleased] — 2026-05-05 (CI cleanup: Node.js 24 migration)

### Changed

- **`.github/workflows/tests.yml` — Node 24 готовность.** GitHub Actions
  начиная с 2026-06-02 принудительно переводит JavaScript-actions на
  Node.js 24, Node.js 20 удаляется 2026-09-16. CI выдавал deprecation
  warning на `actions/checkout@v4`.
  - Все 4 `actions/checkout@v4` → `actions/checkout@v5` (релиз Aug 2025,
    нативно на Node 24).
  - Добавлена `env: FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true` на уровне
    workflow — страхует от любых старых v4-actions через transitive
    composite-actions.

## [Unreleased] — 2026-05-05 (CI fixes: secrets + perms + postgres start_period + state cache TTL)

### Fixed

- **system-e2e fast-mode race: `test_telemetry_suppressed_in_maintenance`.**
  После того как backend-tests поднялся (см. предыдущие фиксы), system-e2e
  ловил race в одном тесте: `test_state_transitions::
  test_telemetry_suppressed_in_maintenance` — телеметрия НЕ должна
  обновлять `current_value` в режиме maintenance, но обновляла. Причина:
  `_STATE_CACHE_TTL = 5.0` (захардкоженный) превышает несколько циклов
  телеметрии в fast-mode (`DEFAULT_TELEMETRY_INTERVAL_S=1`) — REST
  выставлял `operational_status=maintenance`, но WS device_handler ходил
  с устаревшим cached `normal` весь TTL-window → телеметрия суппрессии не
  получала.
  - **`backend/services/locker_api/app/websocket/device_handler.py`:**
    `_STATE_CACHE_TTL` теперь env-driven (`STATE_CACHE_TTL_S`, default
    `5.0`). В prod (`telemetry=30s`) дефолт остаётся оптимальным trade-off.
  - **`backend/.env.fast`:** `STATE_CACHE_TTL_S=1` для fast-mode.
  - **`backend/docker-compose.yaml`:** `locker_api` env прокинут.
  - **`backend/.env.example`:** документирован параметр + контекст.
  - **Verification:** 281 locker_api тестов passing с дефолтом 5.0.

## [Unreleased] — 2026-05-05 (CI fixes: seed secrets + perms + postgres start_period)

### Fixed

- **CI третья стадия: `Permission denied` при чтении `/run/secrets/postgres_password`.**
  После `start_period: 30s` фикса (см. ниже) добавленный log-dump показал
  реальную причину — postgres docker-entrypoint вызывает
  `_load_password_file` УЖЕ от postgres-юзера (uid 999 в контейнере), а не
  от root. Bind-mount секрет-файл сохраняет owner=runner (uid 1001 на
  GitHub Actions) с `chmod 0600` — postgres-юзер не может прочитать
  → `docker-entrypoint.sh: line 21: /run/secrets/postgres_password:
  Permission denied` → постгрес рестартует в loop → unhealthy.
  - **`backend/scripts/seed-dev-secrets.sh`:** chmod изменён с `0600` на
    `0644` (world-readable). Подробный комментарий в скрипте объясняет
    почему. Существующие файлы при повторном запуске тоже получают
    `0644` (idempotent, фиксит унаследованный 0600).
  - **Production:** `DEPLOYMENT.md` сохраняет инструкцию `chmod 600` для
    реальных секретов — там uid файла должен совпадать с uid postgres-
    юзера в контейнере (или использовать другие механизмы доставки
    секретов). Dev-script — только для CI/local-dev с тривиальными
    значениями.

- **CI вторая стадия: postgres healthcheck unhealthy на свежем volume.**
  После seed-секретов (см. ниже) compose стартовал, но `postgres_user`
  падал с `dependency failed: container ... is unhealthy`. Причина:
  healthcheck (`pg_isready`, `interval: 10s, retries: 5`) **без
  `start_period`** начинал probe сразу — на свежем volume postgres ещё
  выполняет initdb + миграцию `001_init.sql`, что на slow CI runner
  занимает 20-30s. 5 retries × 10s = 50s могло не хватить.
  - **Все 5 postgres healthchecks** получили `start_period: 30s`. Это даёт
    initdb + миграциям полные 30s до начала retries-counting (общий
    бюджет ~80s до unhealthy). На существующих volume'ах (где init не
    запускается) overhead — нулевой.
  - **`.github/workflows/tests.yml` `backend-tests` job:** добавлен
    `Dump container logs on failure` step (`if: failure()`) — на следующем
    падении CI сможем сразу увидеть логи каждого postgres/backend сервиса
    без нужды разбираться вслепую.

- **CI `tests.yml` падал на bind-mount `secrets/postgres_password`.**
  PR33 перевёл `postgres_user` на `POSTGRES_PASSWORD_FILE` (file-mount
  через docker compose `secrets:`). Реальные secret-файлы в `.gitignore`
  → свежий CI runner не имел `backend/secrets/postgres_password` →
  docker compose up валился с
  `bind source path does not exist: .../secrets/postgres_password`.
  Тот же блокер проявился ещё ярче после PR37/#1, который добавил
  `POSTGRES_PASSWORD_FILE` явно в env-блок postgres_user.
  - **`backend/scripts/seed-dev-secrets.sh` (новый):** идемпотентный
    bash-скрипт, создаёт 5 dev-default secret-файлов в `backend/secrets/`
    (postgres_password=password, redis_password=`redis-dev-only-do-not-use-in-prod`,
    admin_password=changeme, jwt_secret=`change-me-in-production-must-be-at-least-32-bytes-long`,
    internal_auth_hmac_key=`internal-auth-dev-key-do-not-use-in-prod`).
    Skip если файл уже существует и non-empty. Edge-case-устойчив:
    если на пути директория (Windows + старые docker auto-mkdir-on-bind),
    удаляет и пересоздаёт как файл. Файлы создаются с `chmod 600`.
  - **`.github/workflows/tests.yml`:** добавлен `Seed dev secrets`-step
    в 3 job'а — `backend-tests`, `gateway-tests`, `system-e2e` — перед
    любым `docker compose up`. `emulator-tests` пропущен (не использует
    backend stack).
  - **Production:** скрипт **НЕ для prod**. Документация в скрипте и
    `DEPLOYMENT.md` явно указывает: production использует
    `openssl rand -hex 32` для генерации каждого секрета.
  - **Verification:** скрипт verified локально — все 5 файлов созданы
    корректно.

## [Unreleased] — 2026-05-03 (privilege escalation closed)

### Security

- **PR38 — privilege escalation matrix (security/C-1, последний Critical).**
  Закрытие единственного оставшегося Critical из ревью Sprint 1: до сих пор
  worker мог отправить `POST /api/v1/locker/{id}/commands` с
  `{"type":"reboot"}` или `{"type":"close"}` — `CommandListHandler.post`
  не валидировал `command_type` против `X-Remote-Role`, ACM пропускал
  всех worker'ов с Допуском.
  - **`backend/shared/access_control/commands.py` (новый):** матрица в
    одном месте, чистая Python-функция `check_command_permission(role,
    command_type)`. По решению пользователя 2026-05-03:
      * `worker` → `OPEN`, `CLOSE` (ежедневные операции).
      * `operator` / `administrator` → `OPEN`, `CLOSE`, `REBOOT`, `CONFIG`.
    Default-deny при пустой/неизвестной роли или неизвестном command.
  - **`backend/services/locker_api/app/handlers/command.py`:** в
    `CommandListHandler.post` после парсинга `CommandType` и ДО проверки
    locker existence — `check_command_permission(...)`. На отказ:
    403 + structured warning в логи (`user_id`, `role`, `command_type`,
    `locker_id`) для SOC-аудита. Permission-проверка идёт ДО locker check
    чтобы не раскрывать наличие шкафов пользователям без прав.
  - **`backend/services/acm/app/rules.py`:** обновлён комментарий в блоке
    «Шкафы» — теперь явно ссылается на `shared/access_control/commands.py`
    как источник тонкой матрицы. ACM URL-rule по-прежнему пропускает
    `worker+Допуск`, fine-grained-проверка делегируется в locker_api.
  - **OpenAPI:** в `services/locker_api/openapi/paths/commands.yaml`
    добавлена 403-response с описанием матрицы и ссылкой на
    shared-модуль; в description POST endpoint'а — таблица прав.
  - **Тесты:**
    - `tests/unit/test_command_permission.py` (новый) — 18 тестов:
      все 12 (роль × command) комбинаций + 6 edge cases (пустая роль,
      unknown role, unknown command, None, иерархия subset).
    - `tests/unit/test_command_api.py` — обновлены существующие тесты
      (X-Remote-Role: administrator во все), добавлены 5 новых:
      worker open ✓, worker reboot 403, worker config 403,
      operator reboot ✓, missing role → 403.
    - `tests/integration/conftest.py` — `api_client` дефолт-headers
      включают `X-Remote-Role: administrator`, чтобы integration-тесты
      на REBOOT/CONFIG проходили без явного указания.
  - **Verification:** 280 locker_api тестов passing (было 256, +24 новых)
    + 54 gateway теста.
  - **Post-review fixes (2 High из feature-dev:code-reviewer):**
    1. **Test assertion strengthened.** `test_create_command_worker_reboot/
       config/missing_role_forbidden` — добавлен `db_mock.execute.assert_not_called()`
       (раньше проверялся только `fetch_one`, INSERT через `execute` не
       проверялся — false confidence: если accidentally убрать `return`
       после `write_forbidden`, команда сохранится в БД, но тест прошёл
       бы зелёным).
    2. **Defense-in-depth case-insensitive role.** `command.py`:
       `role = ... .strip().lower()` вместо просто `.strip()`. ACM сейчас
       отдаёт lowercase StrEnum value, но если issuer когда-то напишет
       «Worker» / «WORKER» (миграция, другой инструмент) — мы не отказываем
       легитимному worker'у в `open`/`close`. Матрица хранит только
       lowercase, поэтому safe. Новый тест `test_create_command_role_uppercase_normalized`
       подтверждает поведение: 281 теста passing.

## [Unreleased] — 2026-05-03 (sprint 6 add-on: code-review fixes)

### Fixed

- **PR37 — закрытие 8 High-issue из fresh code-review Sprint 1-6.**
  Запущены 3 параллельных feature-dev:code-reviewer агента по областям
  (auth/secrets, hot-path/concurrency, emulator/observability), confidence-
  filtered результат сведён в один PR. M-1 (state_monitor extra get_by_id)
  отложен — требует refactor сигнатуры `handle_threshold_violation` и
  обновления unit-тестов TestStateMonitorGuards, low-impact.
  - **#1 — `docker-compose.yaml:33` postgres_user отсутствовал
    `POSTGRES_PASSWORD_FILE`.** Все 4 других postgres имели env-строку,
    у postgres_user был только `secrets:` mount + comment. Эффект: на
    fresh deploy с пустым data-volume postgres exits с error «specify
    POSTGRES_PASSWORD». На existing volume init не запускается, всё
    работает (поэтому регрессионные тесты не ловили). Добавлена недостающая
    строка.
  - **#2 — `auth_api/handlers/verify.py:74-90` OTP-burn race.**
    `mark_used(otp.id)` вызывался ДО `_get_user_id_by_phone()`. При
    transient 503 от user_api (rolling restart, network blip) одноразовый
    OTP «сгорал» без выдачи токена — пользователь вынужден заново
    запрашивать SMS. Worse: целевая slow-restart атака на user_api с
    известным phone стопорит auth-flow user'а. Перенёс `mark_used` ПОСЛЕ
    successful resolution user_id. Single-use guarantee сохранена через
    SQL фильтр в `get_active`.
  - **#3 — `user_api/handlers/internal_auth.py:85-105` timing side-channel.**
    Когда HMAC валиден, но user не найден / неактивен / no auth_record —
    return 401 БЕЗ запуска bcrypt.checkpw (~0ms). Когда user найден и
    пароль не подходит — bcrypt запускается (~100ms). Атакующий внутри
    `backend_net` (компрометированный соседний контейнер: acm/history_api/
    notification_api) различает по timing зарегистрированные phone от
    незарегистрированных, обходя anti-enumeration PR16. Fix: dummy
    bcrypt-хэш `_DUMMY_BCRYPT_HASH` (bcrypt.hashpw на placeholder), который
    `checkpw` отрабатывает по полному циклу для отказных веток.
  - **#4 — `shared/auth/base_handler.py:422` UserAuthMixin без super().prepare().**
    В MRO `(UserAuthMixin, JSONHandler, RequestHandler)` `UserAuthMixin.prepare`
    выполнялся первым и не вызывал `super().prepare()` —
    `JSONHandler.prepare` НИКОГДА не запускался → `_metrics_start` не
    выставлялся → `http_request_duration_seconds` histogram записывал 0.0
    seconds для ВСЕХ запросов через user_api/locker_api/history_api
    (отравленные данные observability). Fix: `await super().prepare()` как
    первая строка `UserAuthMixin.prepare`.
  - **#5 — `locker_api/device_handler.py:940` лишний repo.get_by_id в
    `_cleanup_connection`.** Функция вызывала `get_by_id(locker_id)` ради
    одного поля `last_seen_at`, хотя свежий timestamp уже был в БД после
    `update_last_seen + update_connectivity(ONLINE)` в auth-handler. Под
    reconnect storm 1000 устройств это амплифицирует pool pressure. Fix:
    stamp current UTC time на `self.authenticated_locker.last_seen_at`
    сразу после `update_connectivity(ONLINE)`, в `_cleanup_connection`
    использовать кэшированное значение.
  - **#6 — `locker_api/services/command_queue.py:83-95` send_command TOCTOU.**
    `_connections.get(locker_id)` без `_connections_lock`. Между чтением
    handler'а и `await write_message(...)` `register_connection` под lock
    мог заместить supersession'ом и закрыть старый handler с code=4000;
    write на закрытый сокет → `WebSocketClosedError` → false-failed
    dispatch. Fix: snapshot ссылки под lock, write вне lock.
  - **#7 — `emulator/scud_protocol/client.py:222-228, 270-278` deadlock в
    send_telemetry/ping при reconnect.** Owner держит `_telemetry_lock`,
    отправляет, ждёт `_ack_event`. Если транспорт оборвался во время send
    или незадолго до — receive_loop приостановлен в `_authenticate`
    (auth-replay при reconnect), `_ack_event` некому set'нуть, ждём полный
    ack_timeout. На fast-mode (telemetry_interval=1s) spurious
    ProtocolError на каждый reconnect. Fix: проверка `transport.connected`
    перед clear+send, и после TimeoutError — поднимаем TransportError
    вместо ProtocolError, если транспорт оторван.
  - **#8 — `emulator/scud_emulator/provisioning.py:75-80` multi-item silent
    pick.** При HTTP 409 lookup мог вернуть несколько items (race в BE,
    регистр-нечувствительный фильтр), код молча брал `[0]` без warning'а.
    Wrong locker_id → wrong key registered → wrong telemetry attribution.
    Fix: warning лог при `len(items) > 1` для ops-аудита.

### Verification

  Все тесты passing:
  - acm: 133 / auth_api: 71 / history_api: 88 / locker_api: 256 / 
    notification_api: 37 / user_api: 171 / gateway: 54 (810 + 54 = **864**)
  - emulator: **447**
  - Итого: **1311 тестов**, 0 fail.

### Не в scope PR37

- **M-1 state_monitor extra get_by_id** (confidence 80) — требует
  refactor сигнатуры `handle_threshold_violation` (добавление optional
  `locker` параметра) + обновление 4-5 unit-тестов в TestStateMonitorGuards.
  Performance impact на 1000 lockers × ~10 violations/cycle ≈ 10
  SELECTs/min — не критично, отложено в backlog.


## [Unreleased] — 2026-05-02 (sprint 6: production polish)

### Infrastructure / Build

- **PR32 compose hardening (5 audit findings).**
  - **`apache_gateway.depends_on` → `condition: service_healthy`** для всех
    6 backend сервисов (раньше plain list-form, gateway стартовал до того
    как backend готов → 502 на cold start ~30s). Аналогично переведены
    `*_tests` сервисы в long-form `service_healthy`.
  - **Logging rotation:** YAML-anchor `x-logging: &default-logging`
    (`json-file`, max-size 10m, max-file 5) применён к 14 production
    сервисам (5 postgres + redis + 5 backend + acm + apache_gateway +
    backup). Test-контейнеры пропущены (короткоживущие). Защита от
    unbounded log growth → OOM docker host.
  - **`apache_gateway.healthcheck.start_period: 10s`** — раньше healthcheck
    бежал сразу, мог упасть до полного boot Apache.
  - **`history_photos` volume:** проверен — реально используется (mount
    в `history_api` line 282 + `history_api_tests` line 320 +
    `STORAGE_PATH` env + `photo_repository.py:22`). Оставлен.
  - **`locker_api.stop_grace_period: 30s`** (default 10s) — даёт
    `_cleanup_connection` время на graceful WS shutdown 1000 устройств
    через close code 1001 до SIGKILL.

- **PR33 postgres POSTGRES_PASSWORD_FILE.**
  Завершение секретов-стори PR28 (тогда мигрировали только клиентов).
  - **5 postgres-серверов** теперь читают пароль через
    `POSTGRES_PASSWORD_FILE=/run/secrets/postgres_password`. Env
    `POSTGRES_PASSWORD` удалён ИЗ ОТНОШЕНИЯ К postgres-серверам.
  - **Критическая находка:** Astra postgres image inherit upstream
    `file_env()` который **errors с "mutually exclusive"** если оба
    `POSTGRES_PASSWORD` и `POSTGRES_PASSWORD_FILE` заданы. Поэтому keep-both
    стратегия из плана не работает — оставлен только FILE.
    Init-скрипты `init-*-db.sh` re-verified: используют `$POSTGRES_USER`
    и `$POSTGRES_DB` через local socket trust, не читают
    `$POSTGRES_PASSWORD`. Изменений не потребовалось.
  - **Breaking change для fresh deploy:** `backend/secrets/postgres_password`
    обязан существовать перед первым `docker compose up`. Документация
    в `DEPLOYMENT.md` (PR35) содержит команды генерации.
  - **Verification:** 810 backend-тестов passing, manual проверка
    (file-mount → healthy / file-missing на fresh volume → loud failure /
    file-missing на existing volume → стартует с pre-existing pg_authid,
    что correctly reflects postgres init-only поведение).

- **PR34 connection pool env-tuning.**
  Раньше `AsyncPostgres` хардкодил `max_workers=5, min_connections=1` для
  всех сервисов. Под 1000 шкафов с hot-path WS телеметрией `locker_api`
  мог упереться в пул. Теперь параметры env-driven с обратной совместимостью.
  - **`shared/database/connection.py`:** `max_workers`/`min_connections`
    дефолтятся в `None`; при `None` читают `DB_MAX_WORKERS`/`DB_MIN_CONNECTIONS`
    env с fallback `5`/`1`. Явный kwarg по-прежнему перебивает env.
  - **`docker-compose.yaml` per-service tuning:** `locker_api` использует
    отдельные `DB_MAX_WORKERS_LOCKER`/`DB_MIN_CONNECTIONS_LOCKER`
    (default 20/4), `history_api` — `DB_MAX_WORKERS_HISTORY`/`DB_MIN_CONNECTIONS_HISTORY`
    (default 10/2), остальные используют общий `DB_MAX_WORKERS`/`DB_MIN_CONNECTIONS`
    (default 5/1).
  - **`.env.example`:** добавлена таблица рекомендаций по сервисам с
    закомментированными env-vars для prod-tune.
  - **Verification:** 171 user_api + 256 locker_api тестов passing с дефолтами;
    manual проверка `DB_MAX_WORKERS=20` подтверждена (`pool_size=4` warm).

### Observability

- **PR36 Prometheus `/metrics` endpoints (6 backend сервисов).**
  `python3-prometheus-client 0.16.0` подтверждённо есть в Astra `main` apt
  (no extended-only blocker как у PR30 lua-socket).
  - **`backend/shared/metrics/__init__.py` (новый):** общие метрики
    `scud_http_requests_total` (Counter), `scud_http_request_duration_seconds`
    (Histogram, buckets 5ms-10s), `scud_db_query_duration_seconds`
    (Histogram per operation), `scud_ws_active_connections` (Gauge).
    `_normalize_route()` нормализует UUID и numeric IDs в `:id` чтобы
    избежать unbounded cardinality в `route` label.
  - **`backend/shared/metrics/handler.py` (новый):** `MetricsHandler` —
    Tornado `RequestHandler` отдаёт Prometheus exposition format.
    Без auth, доступен только из `backend_net` (Apache не проксирует
    наружу — Option B per security best practice).
  - **HTTP инструментация:** `JSONHandler.prepare()` стартует таймер,
    `JSONHandler.on_finish()` инкрементит counter и observed histogram.
    Wrapped в try/except — никогда не блокирует production traffic при
    сбое метрик. `MetricsHandler` сам не считается (extends `RequestHandler`
    напрямую, минуя mixin).
  - **DB инструментация:** `_run_with_retry_async` в
    `shared/database/connection.py` observed `scud_db_query_duration_seconds`
    с label `operation` ∈ {execute, fetch_one, fetch_all}.
  - **WS инструментация:** `device_handler.open()` инкрементит,
    `on_close()` декрементит `scud_ws_active_connections{service="locker_api"}`
    с `_metrics_inc_done` guard против double-dec.
  - **Dockerfiles:** `python3-prometheus-client` добавлен к 6 backend
    Dockerfile'ам (apt-install строка), `sed '/extended/d'` сохранён.
  - **`SERVICE_NAME` env:** добавлен в env-блок каждого backend сервиса
    в compose — используется как label value.
  - **OpenAPI specs:** `/metrics` endpoint описан в каждом сервисе как
    internal Prometheus scrape endpoint.
  - **Cardinality bound:** route × method × status × 6 services ≈ low
    hundreds series total.
  - **Verification:** все 864 теста (810 backend + 54 gateway) passing.
    Sample `curl http://user_api:8000/metrics` показывает корректный
    exposition format с растущими counter'ами при HTTP-нагрузке.

### Documentation

- **PR35.1 — `backend/DEPLOYMENT.md` (новый).** Production-runbook на русском
  с разделами: Prerequisites (Astra Linux SE 1.7+, Docker 24+, sizing-таблица),
  First-time deployment (clone, .env, openssl-секреты в `backend/secrets/`,
  `docker compose up`, smoke-тесты), TLS termination (nginx-шаблон с WS-upgrade
  для `/api/v1/device/ws` и `X-Forwarded-Proto`), Frontend deployment
  (`FRONTEND_PATH` override), Backup strategy (см. PR35.2), Secret rotation
  (rolling для JWT_SECRET — 24h amortization; синхронная для
  INTERNAL_AUTH_HMAC_KEY с downtime ~1 мин; `ALTER USER` для
  POSTGRES_PASSWORD), Disaster recovery (per-volume restore + полная
  переустановка из off-site backup), Rolling restart
  (`docker compose up -d --no-deps --build user_api`), Observability
  (`/metrics` ссылка на PR36 + Prometheus scrape config + healthchecks +
  логи), Troubleshooting (postgres не стартует, gateway 502, OTP, WS
  reconnect storm, backup-сидекар, INTERNAL_AUTH_HMAC_KEY rotation
  выявил рассинхронизацию).
- **README.md (root):** добавлена ссылка на `backend/DEPLOYMENT.md` и
  раздел "Production deployment" со ссылкой на backup-профиль.

### Infrastructure / Build

- **PR35.2 — Backup-сидекар для 5 postgres.**
  - **`backend/scripts/backup.sh` (новый):** bash-скрипт с `set -euo
    pipefail`, читает пароль из `/run/secrets/postgres_password` (fallback
    PGPASSWORD env), запускает `pg_dump -F c` для всех 5 баз
    (scud_users/scud_lockers/scud_history/scud_auth/scud_notifications),
    складывает в `/backups/<db>-<UTC-timestamp>.dump`, делает rotation
    через `find -mtime +RETENTION_DAYS -delete`. Возвращает exit 2
    если хоть один dump провалился.
  - **`backend/Dockerfile.backup` (новый):** на основе `ubi18-postgresql15`
    (pg_dump уже включён). `cron` отсутствует в сертифицированном Astra
    `main` репозитории, поэтому планировщик — bash-loop с
    `sleep ${BACKUP_INTERVAL_SECONDS}` (default 86400 = раз в сутки).
    Логи дублируются в stdout (docker logs) и `/var/log/backup.log` через
    `tee`. Build делает chown в postgres:postgres, рантайм бежит под
    непривилегированным postgres-юзером.
  - **`docker-compose.yaml`:** новый сервис `backup` под профилем `backup`
    (НЕ запускается при обычном `docker compose up`). depends_on с
    `condition: service_healthy` на все 5 postgres-серверов. Volume
    `backup_data:/backups` (новый top-level volume). Secret
    `postgres_password` смонтирован read-only. Resource limits 256M/0.25
    (легковесный sidecar). Logging anchor `*default-logging` (10M × 5).
  - **Запуск:** `docker compose --profile backup up -d backup` (daemon)
    или `docker compose --profile backup run --rm backup
    /scripts/backup.sh` (one-shot).
  - **Verification:** ручной запуск с поднятыми 5 postgres сделал 5 валидных
    dump-файлов (73-119 KB, формат CUSTOM, 130+ TOC entries для
    scud_users), проверено через `pg_restore --list`.

## [Unreleased] — 2026-05-02 (sprint 5 add-on: Astra certification audit)

### Documentation / Build

- **PR31 Astra certification audit fixes.**
  Аудит после открытия (PR30 spike) что lua-socket доступен только в
  `extended-repository`, не в сертифицированном `main`. Проверены все
  Dockerfile'ы и shell-скрипты на консистентность сертификационного
  pattern'а (`sed '/extended/d'` перед apt install).
  - **`backend/tests/gateway/Dockerfile`:** добавлен `RUN sed -i
    '/extended/d' /etc/apt/sources.list` перед `apt install`. Раньше 4
    устанавливаемых пакета (python3-requests/pytest/bcrypt/psycopg2)
    все были в `main`, но если бы Astra добавила extended в дефолтный
    sources.list — Dockerfile молча начал бы тянуть оттуда. Теперь
    защищён тем же паттерном что и 6 production Dockerfile'ов.
    Дополнительно добавлен `--no-install-recommends` и `apt clean`
    для согласованности.
  - **Docs:** `locker_api/README.md`, `locker_api/DEVELOPER_GUIDE.md`,
    `user_api/DEVELOPER_GUIDE.md` — убраны инструкции `pip install -r
    requirements.txt` для локального запуска. Заменены на Docker-only
    workflow с явным предупреждением: backend-сервисы зависят от
    сертифицированных Astra-пакетов, pip из PyPI ставит другие версии и
    не сертифицирован. `requirements.txt` остаются как индикатив для
    IDE-автокомплита (production источник истины — `apt install` в
    Dockerfile).
  - **`CONVENTIONS.md`:** новый раздел про Astra certification — explicit
    объяснение `main` vs `extended` (только `main` сертифицирован),
    обязательность `sed '/extended/d'` в Dockerfile'ах, запрет pip-install
    в backend, инструкция что делать если пакет в extended (стdlib
    альтернатива, как `shared/redis_client.py` для python3-redis;
    либо deferred с research note, как PR30 для lua-socket).
    Раньше это было tribal knowledge — теперь явно зафиксировано.
  - **Verification:** `gateway_tests` 54/54 passed после изменения
    Dockerfile.

## [Unreleased] — 2026-05-02 (sprint 5: production hardening, emulator H-items, Lua spike)

### Infrastructure
- **PR28 production hardening compose.**
  - **Restart policies:** добавлен `restart: unless-stopped` к 5 postgres-сервисам
    (раньше был только у redis и backend-сервисов; postgres перезапускались
    только при `docker compose up`).
  - **Resource limits:** все 8 production-сервисов получили
    `deploy.resources.limits` + дублирующие `mem_limit`/`cpus` (top-level v2)
    для гарантированного enforcement. Постгрессы 512M/0.5cpu, redis
    256M/0.25cpu, обычные backend 1G/1.0cpu, locker_api (WS 1000 устройств)
    2G/2.0cpu, gateway 512M/0.5cpu. Test-контейнеры лимиты не получили
    (короткоживущие).
  - **Frontend path → env var:** `${FRONTEND_PATH:-/root/frontend}` вместо
    захардкоженного пути; `.env.example` документирует override для prod.
  - **Docker secrets file-mount migration:** новый top-level `secrets:` блок
    в compose с file-driven секретами (`postgres_password`, `jwt_secret`,
    `admin_password`, `internal_auth_hmac_key`, `redis_password`). Помощник
    `read_secret(name, *, env_var, default)` в `shared/utils/__init__.py`
    читает сначала `/run/secrets/<name>`, затем env, затем default — env-fallback
    сохраняет совместимость dev/integration tests. Postgres-серверы и redis
    оставлены на env (init-скрипты postgres не читают из файла; redis
    требует `--requirepass` arg). Только клиенты (5 backend сервисов)
    переведены на read_secret. `backend/secrets/.gitkeep` placeholder;
    `.gitignore` исключает реальные secret-файлы.
  - **Verification:** все 810 backend-тестов проходят и в env-fallback,
    и в file-mount режиме. `docker inspect` показывает что лимиты применены.

### Emulator
- **PR29 emulator H-items.**
  - **H-1 idempotent provisioning:** `provisioning.py:59` при HTTP 409 теперь
    делает `GET /api/v1/lockers?inventory_number=<X>` для recovery
    `locker_id`, после чего продолжает с регистрацией ключа и сенсоров.
    Раньше silent return оставлял шкаф без id → `start()` падал с
    `RuntimeError("Locker not provisioned")`. 5 новых тестов
    (`test_emulator/test_provisioning.py`).
  - **H-2 asyncio.Lock:** `client.send_telemetry`/`ping` обёрнуты в
    `_telemetry_lock`/`_ping_lock`, защищая `clear()/send/wait()` пары на
    общих `_ack_event`/`_pong_event`. `transport.send` обёрнут в
    `_send_lock` чтобы предотвратить interleaving WS-кадров при
    параллельных вызовах. 3 новых concurrent-теста в
    `tests/integration/test_client_integration.py`.
  - **H-3 fleet task tracking:** `LockerFleet._fleet_tasks: set[asyncio.Task]`
    теперь хранит созданные `start_all`-задачи с `add_done_callback(discard)`.
    `stop_all` дренирует pending задачи через `asyncio.gather(...,
    return_exceptions=True)`. Раньше задачи могли быть собраны GC'ом
    (нарушение PEP 3156). 3 новых теста в `test_fleet_integration.py`.
  - **Verification:** 447 emulator-тестов проходят (было 436, +11 новых).

### Performance
- **PR30 Lua native HTTP client — заблокирован, откатился.**
  - **Spike-результат:** `mod_lua` не имеет нативного httpclient API.
    `lua-socket 3.1.0-1+b1` доступен только в Astra
    `extended-repository/main` — **НЕ в сертифицированном `main`**, на
    который ориентируется `available_packets.txt` (правило проекта).
    `grep -i lua available_packets.txt` → 0 результатов.
  - **Откат:** `backend/gateway/Dockerfile` и `backend/gateway/lua/jwt_auth.lua`
    восстановлены из `git HEAD` (то есть до PR30). Production остаётся
    на `io.popen("curl ...")` с `shell_quote()` (PR4) и `--max-time 2`
    (PR14).
  - **Documentation:** `docs/research/lua-httpclient.md` обновлён —
    задокументирован спайк, найденная блокировка (extended-only) и
    долгосрочные пути (мониторинг появления в `main`, nginx+OpenResty,
    сидкар-демон, mpm_event).
  - **Что осталось из митигаций:** PR6 singleflight + PR14 `--max-time 2`
    + `shell_quote()` PR4. Fork-overhead сохраняется, но катастрофичные
    сценарии (cascade при деградации ACM) ограничены.

## [Unreleased] — 2026-05-02 (sprint 3+4: тесты, security medium, docs, emulator)

### Tests
- **PR17 ACM CheckHandler — 11 unit-тестов** (`backend/services/acm/tests/unit/test_check_handler.py`).
  Покрывает 7-шаговый алгоритм авторизации: missing X-Request-Path → 403,
  public path bypass, missing/invalid token → 401, cache miss → loader,
  ctx.state≠ACTIVE → invalidate+403, unknown role → 403, role-deny → 403,
  worker locker scope, operator wildcard, full_access. Покрытие `check.py`
  поднялось с 0% до ~95%.
- **PR18 auth_api revoke_user — 8 unit-тестов** (`tests/unit/test_revoke_user.py`).
  Все ветки `RevokeUserTokensHandler.delete`: invalid target, missing/non-numeric
  X-Remote-User, UserApiUnavailable → 503, role None → 401, operator/worker → 403,
  admin → revoke + 200.
- **PR19 notification_api internal_notification — 7 unit-тестов**
  (`tests/unit/test_internal_notification.py`). POST: invalid body (dacite
  MissingValueError) → 400, unknown NotificationType → 400, happy path 201.
  GET: filter-arguments прокидываются в repo. DELETE: 404/200.
- **PR20 user_api internal_auth — 18 unit-тестов** (`tests/unit/test_internal_auth.py`).
  `InternalAuthCheckHandler` (11 тестов: empty body, missing fields, user not
  found, state≠ACTIVE, missing auth_record, bcrypt raises, wrong pw, happy
  path с/без 2fa). `InternalUserByPhoneHandler` (4). `InternalUserByIdHandler` (3).

- **PR21 shared/test_fixtures — дедупликация интеграционных conftest'ов.**
  Создан `backend/shared/test_fixtures/__init__.py` (142 LOC) с параметризованными
  helpers: `wait_for_db(host, port, ..., probe_table)`, `wait_for_api(base_url,
  probe_path, accepted_statuses=...)`, `APIClient(base_url, default_headers)`.
  Шесть `tests/integration/conftest.py` сократились с 937 до 628 LOC
  (-309 LOC). Сервис-специфичные фикстуры (acm AcmClient, auth_api OTP/admin
  setup, history_api uuid generators, user_api db_connection) остались локально.
  Все 725 интеграционных тестов в 6 сервисах зелёные.
- **PR22 CI coverage — заблокировано на Astra.** В `available_packets.txt`
  отсутствуют `python3-coverage` и `python3-pytest-cov`; pip-установка в
  production Dockerfile запрещена (правило проекта). В `.github/workflows/tests.yml`
  оставлен TODO-блок c тремя путями разблокировки. Существующая `pytest`
  matrix продолжает работать без изменений.

### Security
- **PR23 HMAC request signing между auth_api и user_api**
  (security/M-2). Re-verify показал: `set_password_hash` применяет bcrypt
  поверх SHA-256 (см. `repositories/user.py:298-301`), поэтому простая замена
  формата хэша требует пересева всех паролей. Выбрана узкая, более сильная
  альтернатива — request signing.
  - `shared/utils.compute_internal_signature(timestamp, phone, password_hash, key)`
    HMAC-SHA256 канонической строки `timestamp\nphone\npassword_hash`.
  - `verify_internal_signature(...)` использует `hmac.compare_digest` и
    проверяет окно 30 секунд (replay protection).
  - `auth_api.UserApiClient` подписывает каждый `check_credentials` запрос
    заголовками `X-Internal-Timestamp`, `X-Internal-Signature`.
  - `user_api.InternalAuthCheckHandler` верифицирует подпись ПЕРЕД любым DB
    lookup; невалидная/просроченная/отсутствующая → 401 без тела.
  - Shared secret через env `INTERNAL_AUTH_HMAC_KEY`. Дефолт в compose
    `internal-auth-dev-key-do-not-use-in-prod`; `.env.example` требует
    регенерацию для prod (`openssl rand -hex 32`). 21 unit-тест на helpers
    + 6 на handler.
  - `hash_password` намеренно сохранён — используется в user-creation flow
    (storage остаётся `bcrypt(SHA-256(password))`). Атакующий на `backend_net`
    больше не может replay-нуть перехваченный `password_hash` без ключа.
- **PR24 WS auth phase 1 rate-limit** (security/M-8).
  `device_handler._handle_auth` теперь проверяет per-IP TokenBucket ПЕРЕД
  любым DB-lookup. Дефолты: capacity 5 / refill 0.0833 (≈ 5 попыток/мин).
  При превышении — `auth_error` с unified `_AUTH_FAILED_MESSAGE` (anti-enumeration
  PR16 сохранён) + `close(code=4429, reason="rate_limit")`.
  - In-memory `OrderedDict` (LRU, cap=10000, evict ~1000) — защита от
    unbounded memory growth от уникальных source-IP.
  - Compose-дефолты повышены (`WS_AUTH_RATE_LIMIT=1000`/`REFILL=0.1`) чтобы
    integration-тесты не упирались; `.env.example` рекомендует prod-значения.
  - Тестовая изоляция через `_AUTH_BUCKETS.clear()` в `setUp`. Новый файл
    `tests/integration/test_websocket_rate_limit.py` (4 теста, в том числе
    burst-overflow → close(4429), DB не вызывается при rate-limit, LRU eviction).
- **PR25 CORS env wiring починен** (security/H-3).
  Re-verify обнаружил: PR14 добавил `${CORS_ALLOWED_ORIGIN}` в `apache.conf`
  и sed-replace в `entrypoint.sh`, но `apache_gateway` сервис в
  `docker-compose.yaml` не имел `environment:` блока — env-переменная не
  доходила до контейнера. Wiring был мёртвый. Добавлен env-блок с
  `CORS_ALLOWED_ORIGIN=${CORS_ALLOWED_ORIGIN:-*}`. `.env.example` документирует
  prod-override `https://scud.example.com` с предупреждением о
  несовместимости `*` с `Access-Control-Allow-Credentials` + Secure cookies.
  Acceptance: preflight с `Origin: https://example.com` → ровно этот origin
  в Access-Control-Allow-Origin.

### Documentation
- **PR26 docs hygiene.**
  - **H-1:** `openapi_device.md:226` `PATCH /api/v1/locker/{id}/public-key`
    → `POST /api/v1/locker/{id}/set-public-key` (соответствие реальному
    маршруту в `acm/rules.py:125`).
  - **H-2:** `user_api/README.md` — убраны инструкции локального `pytest`
    (нарушали соглашения проекта), оставлен только `docker compose --profile tests up
    --build user_api_tests`.
  - **H-3:** `REDIS_URL` добавлен в env-таблицы `acm/README.md` и
    `user_api/README.md` со ссылкой на `REDIS_PASSWORD` и `.env.example`.
  - **H-4:** `history_api/openapi/openapi.yaml` — `$ref`-ссылки на 4
    internal endpoints из `paths/internal.yaml`.
  - **M-1:** `openapi_device.yaml` 3.1.0 → 3.2.0.
  - **M-2:** `DevNotes.md` (8-строчный скелет) удалён, ссылки убраны из
    `INDEX.md`, `README.md`.
  - **M-3:** опечатка «taberлица» → «таблица» в `INDEX.md:14`.
  - **M-4:** `(TODO)`-маркеры из `locker_api/README.md` для уже сделанных
    интеграций Notification API и History API.
  - **M-5:** уже закрыто в более раннем sprint — `shared/dtos.py` есть,
    все сервисы импортируют `PaginatedResponse` оттуда.

### Emulator
- **PR27 emulator critical fixes.**
  - **C-1:** `scud locker list` падал с TypeError. `scud_cli/client.py:43`
    возвращал raw `resp.json()`, но эмулятор-сервер отдаёт
    `PaginatedLockerResponse({items, total, limit, offset})`. CLI
    `print_lockers` итерировал по string-keys и крэшился. Fix: `list_lockers`
    возвращает `resp["items"]` с graceful fallback на bare list. 3 теста.
  - **C-2:** `MetricsCollector.reconnects` всегда 0 — `inc_reconnects`
    определён, но `WebSocketTransport._reconnect` его не вызывал. Fix:
    добавлен опциональный callback `on_reconnect_success: Callable[[], None]`
    в конструкторе transport (decoupled — не тянет MetricsCollector в
    `scud_protocol`); `VirtualLocker.start()` пробрасывает
    `metrics.inc_reconnects` как callback. 5 новых тестов.
  - 436 emulator-тестов проходят (было 429, +7).

## [Unreleased] — 2026-05-02 (security sprint)

### Security
- **docker-compose: Redis с `requirepass`, ACCESS_TOKEN_TTL 720h → 24h, SECURE_COOKIES в env.**
  - `redis` запускался с `--protected-mode no` без пароля; любой контейнер
    в `backend_net` мог читать ACM-кэш с ролями активных пользователей.
    Теперь `--requirepass ${REDIS_PASSWORD}` (default-dev `redis-dev-only-do-not-use-in-prod`).
    `REDIS_URL` собирается с паролем для user_api и acm.
  - `ACCESS_TOKEN_TTL_HOURS=720` (30 дней) → дефолт `24` через `${ACCESS_TOKEN_TTL_HOURS:-24}`.
    Окно компрометации токена в системе контроля физического доступа сокращено с месяца до суток.
  - `SECURE_COOKIES=false` больше не захардкожен — `${SECURE_COOKIES:-false}`.
    Дефолт `false` оставлен для dev/test (HTTP), `.env.example` явно требует
    `SECURE_COOKIES=true` для prod.
  - `VERIFY_RATE_LIMIT=100` (compose) / `10` (prod через `.env`) — см. ниже.

- **gateway: убран `mod_status` с `/health`, добавлены security headers, X-Real-IP forwarding.**
  - `/health` отдавал `SetHandler server-status` — публично раскрывал
    версию Apache, worker-state, IP клиентов, URL обрабатываемых запросов
    (включая чужие). Заменено на minimal mod_lua handler `health.lua` с
    телом `ok`. `mod_status` отключён в Dockerfile.
  - Заголовки: `Strict-Transport-Security` (env=HTTPS), `Content-Security-Policy`,
    `Permissions-Policy`. CORS-Origin теперь env-driven (`${CORS_ALLOWED_ORIGIN}`)
    через `entrypoint.sh` + `sed`-replace на старте gateway-контейнера.
  - `RequestHeader set X-Real-IP "%{REMOTE_ADDR}s"` — реальный IP клиента
    проброшен в backend, иначе rate-limit в auth_api работал по IP gateway-контейнера
    (один IP на всех клиентов).
  - Lua `--max-time 5` → `2` — Apache-воркер быстрее освобождается при
    деградации ACM (PR6 singleflight уже снизил blast radius).

- **auth_api: rate-limit на `/auth/verify` + Tornado `xheaders=True`.**
  Защита от OTP brute-force поверх `otp_max_attempts=3` (тот закрывает
  только перебор одного OTP-кода, не серию OTP-сессий). Дефолт compose
  `100/60s`, в production через `.env` рекомендуется `10/60s`.
  `xheaders=True` добавлен во все 6 сервисов — Tornado читает X-Real-IP
  / X-Forwarded-For как `request.remote_ip` (нужно для per-client rate-limit
  и для логирования настоящего IP в WS-handler).

- **locker_api WS: max_message_size 256 КиБ + anti-enumeration в auth.**
  Раньше `on_message` принимал любой размер payload — злоумышленник мог
  отправить многомегабайтное сообщение или base64-decode bomb. Tornado
  `max_message_size = 262144` закрывает это автоматически (закрытие с
  кодом 1009 Message Too Big).
  Phase 1 auth-handler возвращал разные тексты для unknown locker_id /
  missing public_key — позволяло перебирать существующие locker_id.
  Унифицировано в `Authentication failed`; реальная причина пишется
  только в логи.

## [Unreleased] — 2026-05-02

### Changed
- **user_api: bare-литералы `'deleted'`/`'deactivated'`/`'active'` в SQL заменены на `UserState.*.value`.**
  Соответствие правилу `CONVENTIONS.md` «константы — только enum / module-level». Все
  6 точек в `user_data/db/repositories/user.py` теперь параметризованы.

- **user_api: `reactivate` — 2 round-trip вместо 3.**
  Один `UPDATE ... WHERE state <> 'active' RETURNING id` + один финальный
  `get_by_id` для возврата объекта с `contractor_name` через JOIN.
  Раньше: SELECT (existing check) + UPDATE + SELECT (return).

- **user_api: `UserHandler.delete` теперь явно `self.finish()` после `set_status(204)`.**

- **locker_api: `LockerListHandler.get` использует `parse_pagination_params`.**
  Ручной `int(get_query_argument(...))` падал на нечисловом вводе с 500
  и не валидировал `max_limit`.

- **notification_api: SMS scheduler в stub-mode не помечает сообщения как sent.**
  Без `SMS_PROVIDER_URL` планировщик логирует одно warning на старте и
  не дотрагивается до pending/periodic очередей. Раньше при запуске с
  TODO-stub отметка `mark_one_time_sent`/`advance_periodic` происходила
  без реальной отправки — сообщения молча пропадали.

- **shared: чистка пакета.**
  - `shared.auth.__init__` экспортирует `ProblemDetailsHandler` и `UserAuthMixin`
    (раньше сервисы импортировали из приватного `shared.auth.base_handler`).
  - Удалён пустой `shared.logging` (никто не использовал, был заглушкой).

- **auth_api: SMS отправляется через `RetryHTTPClient` (singleton).**
  Раньше использовался прямой `tornado.httpclient.AsyncHTTPClient` —
  CONVENTIONS.md явно запрещает: при первом 5xx/network ошибки SMS
  теряется. Теперь — singleton `sms_client` в settings, retry на
  сетевые сбои и 5xx.

- **production-код: `datetime.utcnow()` → `datetime.now(timezone.utc)`.**
  6 production-вхождений (auth_api/handlers/login,access,verify,
  shared/http/problem, locker_api/.../telemetry_producer). Naive datetime
  плохо сочетается с TIMESTAMPTZ в PostgreSQL и deprecated в Python 3.12+.

- **shared: единый `BaseHandler` через `ProblemDetailsHandler.get_json_body_as`.**
  Раньше каждый из 4 сервисов (locker_api / notification_api / history_api /
  user_api) держал свою копию `get_json_body_as` — два sync, два async, с
  разными исключениями. Унифицировано в shared (sync, dacite, при ошибке
  валидации — `write_validation_error` + `Finish`). Удалены дубликаты в
  4 файлах handlers/base.py. История API: убрано переопределение `write_json`
  с `default=str` в пользу shared `_json_serializer` (datetime/dataclass).
  Все вызовы `await self.get_json_body_as(...)` в user_api/history_api
  переписаны как sync (~13 точек).

### Fixed (locker_api)
- **on_close → update_connectivity(OFFLINE) больше не сбивает свежее переподключение.**
  `CommandQueue.unregister_connection` теперь возвращает `bool` — был ли
  handler действительно удалён. `_cleanup_connection` пропускает шаги
  cleanup (UPDATE OFFLINE, DELETE device_connections, OFFLINE event), если
  другой WS успел перехватить locker_id за время от `on_close` до выполнения
  callback'а. Параллельно: на старте `main.py` сбрасывает в OFFLINE шкафы,
  оставшиеся ONLINE после нечистого шатдауна (новый `reset_stale_online_lockers`).

- **NotificationClient теперь singleton.**
  Раньше `_notify_locker_opened`/`_notify_locker_closed`/`StateMonitor._send_notification`
  создавали новый `NotificationClient` (и под капотом `RetryHTTPClient`) на
  каждый вызов — лишний GC pressure на каждое WS-сообщение. Теперь один
  инстанс создаётся в `main.py` и передаётся в `DeviceWebSocketHandler` и
  `StateMonitor` через `application.settings`/конструктор.

- **`_send_pending_commands` устойчив к разрыву WS и батчит UPDATE.**
  `write_message` обёрнут в `try/except WebSocketClosedError`: если соединение
  оборвалось в середине отправки, оставшиеся команды **не** помечаются как
  `sent` и доставляются в следующей сессии. После успешного цикла —
  один `update_status_to_sent_batch(ids)` вместо N round-trip.

### Performance
- **history_api: оптимизированы analytics-запросы и Apache REST timeout.**
  - `get_locker_summary` объединён в один SELECT с window-функциями вместо двух
    последовательных (раньше второй запрос дублировал scan по партициям только
    ради `MIN/MAX(recorded_at)`).
  - `get_count` без явного date range теперь дефолтно ограничен последними
    30 днями, чтобы COUNT(*) не сканировал все 36+ месячных партиций.
  - `apache.conf`: REST `ProxyPass` timeout снижен с 300 c до 30 c (17 правил).
    WebSocket /api/v1/device/ws оставлен на 3600 c. Зависший REST-запрос
    больше не держит Apache-воркер 5 минут.

- **locker_api: добавлен LIMIT в `get_pending_commands` без `locker_id`.**
  Без него фоновый command_queue загружал всю pending-очередь в память
  (10K шкафов × 3 команды × ~500 байт = ~15 МБ на итерацию). Теперь батч
  по умолчанию 500 строк, обрабатываемый сериями.

- **acm: singleflight против cold-cache stampede.**
  При рестарте Redis или ACM 500 одновременных запросов от 100 пользователей
  раньше шли все в БД параллельно (100×2 SQL = 200 одновременных запросов;
  при пуле maxconn=5 первые 5 проходили, остальные получали PoolError).
  Добавлен `UserContextCache.get_or_load(user_id, loader)` со словарём
  in-flight future: при cache miss только одна корутина грузит, остальные
  ждут её результат. Добавлены unit-тесты на конкурентные miss-ы,
  loader→None и пропагацию исключений.

- **locker_api: StateMonitor больше не делает N+1 SELECT.**
  Раньше каждые 60 c сервис сначала получал список всех датчиков с активными
  порогами (1 SELECT), потом делал отдельный `SELECT FROM sensor_thresholds`
  на каждый sensor_id (5K SELECT/итерацию при 1000 шкафов × 5 датчиков =
  ~80% насыщения 5-коннект пула на 2 c только под StateMonitor).
  Теперь — один JOIN-запрос возвращает все (sensor, threshold)-пары, проверка
  выполняется in-memory через новую `ThresholdRepository.evaluate_thresholds`
  (чистая функция, доступна и из других мест).

- **locker_api: hot-path WS-телеметрии — устранены 3 лишних round-trip на батч.**
  - `SensorRepository.update_value` больше не делает `SELECT ... JOIN sensor_types`
    после `UPDATE`: при 1000 шкафов × 10 датчиков × 10 батчей/с это убирает
    ~10K SELECT/с из пула.
  - В `DeviceWebSocketHandler` добавлен TTL-кэш (5 c) для `door` /
    `operational_status`: вместо `SELECT * FROM lockers` на каждый
    телеметрический батч (~10K SELECT/с при той же нагрузке) — освежение
    раз в 5 c. Door обновляется явно при `command_result` OPEN/CLOSE.
  - В Ed25519-handshake phase 2 убран повторный `LockerRepository.get_by_id`:
    объект `Locker` сохраняется из phase 1 в `self._pending_locker` и
    переиспользуется для проверки подписи.
  - `_handle_ping` дросселирует `UPDATE device_connections.last_ping_at`
    до раза в 60 c (раньше — UPDATE на каждый ping; 1000 шкафов × ping/30 c
    = 33 UPDATE/с на одну строку).

### Fixed
- **auth_api: graceful shutdown теперь дожидается завершения `cleanup_loop`.**
  Раньше `cleanup_task.cancel()` вызывался без `await`, и `db.close()`
  закрывал пул пока активная транзакция `delete_expired()` ещё выполнялась
  в executor — это приводило к ошибкам при штатной остановке контейнера.
  Добавлен `await asyncio.gather(cleanup_task, return_exceptions=True)`
  по аналогии с другими сервисами.

- **history_api: добавлен `wait_for_db()`, `logging.basicConfig` перенесён в `main()`.**
  Сервис был единственным, который создавал `AsyncPostgres` напрямую без
  retry-цикла, и при сваленной БД на старте контейнера сразу падал.
  Заодно `logging.basicConfig` больше не вызывается на уровне модуля —
  устраняет побочный эффект при импорте в тестах.

- **shared: retry в `AsyncPostgres` переведён на async, `pool_acquire_timeout_sec` теперь работает.**
  Внутри `_execute_with_retry` использовался `time.sleep`, который удерживал
  рабочий поток `ThreadPoolExecutor` на весь backoff (до 5 c × 3 попыток);
  при PostgreSQL-флапе пять параллельных retryable-ошибок укладывали весь пул.
  Заменено на новый `_run_with_retry_async` с `await asyncio.sleep` —
  ни event loop, ни рабочие потоки не блокируются между попытками.
  Параллельно: `psycopg2.pool.PoolError("connection pool exhausted")`
  переводится в `ConnectionPoolExhaustedError`, и async-обёртка ждёт
  освобождения пула с polling-интервалом 0.05→0.5 c в пределах
  `pool_acquire_timeout_sec` (раньше параметр был объявлен, но не применялся).
  Затрагивает все 6 сервисов через `shared.database.AsyncPostgres`.

- **history_api: автоматическое создание партиций `sensor_telemetry` + DEFAULT-партиция, чистка миграции.**
  Раньше `001_init_history.sql` создавал 36 статических месячных партиций
  на 2024-2026; последняя покрывала только до 2027-01-01. `RetentionService`
  параллельно создавал 3 месяца вперёд, но не страховал текущий месяц —
  INSERT в неподдерживаемую дату упал бы с ошибкой PostgreSQL.
  - Lookahead в `RetentionService._create_future_partitions` увеличен 3→6
    и теперь включает текущий месяц (диапазон `range(0, LOOKAHEAD_MONTHS + 1)`).
  - В `history_api/main.py` `_create_future_partitions()` вызывается
    **синхронно перед `app.listen()`** — иначе ранние INSERTs полетят в
    DEFAULT-партицию до первой итерации фонового loop'а.
  - Добавлена `sensor_telemetry_default` PARTITION OF ... DEFAULT — safety
    net на случай отказа сервиса. Попадание строк туда — индикатор инцидента.
  - **Удалены** 36 статических партиций 2024-2026 из миграции — они
    дублировали автоматику и устаревали бы каждый год. RetentionService
    создаёт всё что нужно при первом старте.
  - **Применение на dev/prod с уже накатанной 001:** выполнить вручную одну
    команду через `psql` к `scud_history`:
    ```sql
    CREATE TABLE IF NOT EXISTS sensor_telemetry_default PARTITION OF sensor_telemetry DEFAULT;
    ```
    Существующие партиции 2024-2026 в БД остаются — миграция не удаляет их;
    `RetentionService._drop_old_partitions` снесёт их по мере выхода за
    `retention_months`. Остальное — изменения в Python-коде.

## [Unreleased] — 2026-04-29

### Security
- Закрыт Cluster A: trust boundary между gateway и сервисами, обход
  port-binding, ограничение слушающих интерфейсов.
- Закрыт Variant 1: устранена Lua shell injection в gateway
  (`backend/gateway/lua/jwt_auth.lua`); из логов `auth_api` убрана утечка
  OTP-кодов.

### Changed
- Закрыт Cluster B (Architecture): унифицированы retry-политика и
  `ProblemType` для inter-service HTTP-вызовов в четырёх сервисах.
- Закрыт Cluster C (Code quality): введена единая discipline для
  констант — добавлен `backend/shared/enums.py`, локальный
  `SensorValueType`; SQL-литералы параметризованы.

### Added
- Объявлены ранее неявные зависимости: `cryptography`, `bcrypt`, `PyJWT` —
  добавлены в `requirements.txt` соответствующих сервисов.

### Documentation
- Закрыт Cluster F: проведён сплошной sweep документации согласно
  отчётом код-ревью документации (`docs/review/`) — 21 issue, 16 изменённых и 2 новых файла
  (включая корневые `README.md` и этот `CHANGELOG.md`).

## История до 2026-04-28: см. `git log`.
