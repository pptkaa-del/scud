# Relay Board MQTT Protocol

Спецификация MQTT-протокола для подключения релейных плат шкафов
(локеров) к Locker API. Это единственный транспорт устройств: релейные
платы общаются с backend через брокер Mosquitto, все шкафы подключаются
по этому MQTT-протоколу.

- **Транспорт**: MQTT 3.1.1 поверх брокера `mqtt` (Mosquitto 2.0.23,
  `mqtt:1883` в `backend_net`). В production брокер слушает только loopback.
  Внешне устройство подключается либо на `1883` без TLS, либо на `8883` с TLS 1.2
  через терминатор `mqtt_tls` (haproxy); внутри `backend_net` трафик в обоих
  случаях остаётся plaintext.
- **Схемы топиков**: устройство публикует либо в общие `stat` / `rep` / `photos`,
  либо в per-id `stat/{id}` / `rep/{id}` / `photos/{id}` (ПИВ v2). Бэкенд
  подписан на обе; при наличии суффикса он обязан совпасть с `device_id` внутри
  кадра, иначе кадр отбрасывается как подделка.
- **Кодирование**: бинарные кадры, все числовые поля — `uint32`
  **little-endian** (`U32 = 4` байта).
- **Авторизация**: per-device логин/пароль + ACL в брокере (см. §6).

> Документ синхронизирован с `app/mqtt/` (`relay_protocol.py`,
> `relay_codec.py`, `ingest.py`, `results.py`, `transport.py`,
> `provisioning.py`, `offline_monitor.py`). При изменении протокола
> обновлять код и этот документ одновременно.

---

## 1. Топики

Каждый релей идентифицируется полем `device_id` **внутри** payload, а не
топиком: `stat`/`rep`/`photos` — общие топики, в которые пишут все платы.
Команды — в персональный топик `com/{relay_id}`.

| Топик            | Направление       | QoS | Retain        | Назначение                       |
|------------------|-------------------|-----|---------------|----------------------------------|
| `stat`           | плата → backend   | —   | —             | Телеметрия и статус устройства   |
| `rep`            | плата → backend   | —   | —             | Результат выполнения команды     |
| `photos`         | плата → backend   | —   | —             | Кадр фото (заголовок + тело)     |
| `com/{relay_id}` | backend → плата   | 1   | условно (§4)  | Команда конкретной плате         |

Backend публикует команды всегда с **QoS 1** (`AT_LEAST_ONCE`). Флаг
retain зависит от типа команды (см. §4).

---

## 2. Перечисления и константы

Источник: `relay_protocol.py`.

**Command** (поле `command` в `com`):

| Имя            | Код | Версия |
|----------------|-----|--------|
| `NONE`         | 0   |        |
| `RELAY_ON`     | 1   |        |
| `RELAY_OFF`    | 2   |        |
| `RELAY_REBOOT` | 3   |        |
| `SEND_STATUS`  | 4   |        |
| `LOCK_OPEN`    | 5   | v2     |
| `LOCK_CLOSE`   | 6   | v2     |

**RepResult** (поле `result` в `rep`): `SUCCESS=1`, `FAILURE=2`,
`NOT_SUPPORTED=3`.

**DeviceStatus**: `OFFLINE=0`, `ONLINE=1`, `ERROR=2`.
**DoorStateRaw** (v2): `UNDEFINED=0`, `OPEN=1`, `CLOSED=2`.
**PowerSource** (v2): `BATTERY=0`, `MAINS=1`.
**ProtocolVersion**: `V1=1`, `V2=2`.

Длины кадров: `REP_LEN=12`, `STAT_V1_LEN=36`, `STAT_V2_NORELAY_LEN=28`,
`STAT_V2_RELAY_LEN=52`.

---

## 3. Форматы сообщений

Все смещения — в байтах от начала payload. Каждое поле — `uint32` LE,
если не указано иное.

### 3.1 `com` — команда (backend → плата)

12 байт, кодировщик `encode_com`:

| Смещение | Поле      | Описание                                         |
|----------|-----------|--------------------------------------------------|
| 0        | `cmd_id`  | `relay_cmd_id` (uint32), эхом возвращается в `rep`|
| 4        | `command` | Код из `Command`                                 |
| 8        | `data`    | Доп. параметр (default 0; индекс реле-канала)    |

### 3.2 `rep` — результат команды (плата → backend)

Ровно `REP_LEN = 12` байт (иначе кадр отбрасывается):

| Смещение | Поле        | Описание                                  |
|----------|-------------|-------------------------------------------|
| 0        | `device_id` | Идентификатор платы                       |
| 4        | `cmd_id`    | Эхо `relay_cmd_id` из соответствующего `com`|
| 8        | `result`    | Код из `RepResult`                        |

### 3.3 `stat` — телеметрия/статус (плата → backend)

Дискриминатор версии — поле `version` на смещении 4. Минимум 8 байт на
заголовок (`device_id` + `version`).

**v1** (`version=1`, ≥ 36 байт):

| Смещение | Поле        |
|----------|-------------|
| 0        | `device_id` |
| 4        | `version` (=1) |
| 8        | `relays[0]` |
| 12       | `relays[1]` |
| 16       | `relays[2]` |
| 20       | `relays[3]` |
| 24       | `status` (`DeviceStatus`) |
| 28       | `signal`    |
| 32       | `alarm`     |

**v2** (`version=2`): дополнительный дискриминатор — `relay_en` на
смещении 24 (≥ 28 байт). Общие поля v2:

| Смещение | Поле        |
|----------|-------------|
| 0        | `device_id` |
| 4        | `version` (=2) |
| 8        | `door` (`DoorStateRaw`) |
| 12       | `power` (`PowerSource`) |
| 16       | `battery`   |
| 20       | `net_type` (`NetType`: 0 — не определён, 1 — Ethernet, 2 — GSM) |
| 24       | `relay_en`  |

**v2, `relay_en=1`** (реле-блок присутствует, ≥ 52 байта):

| Смещение | Поле        |
|----------|-------------|
| 28       | `relays[0]` |
| 32       | `relays[1]` |
| 36       | `relays[2]` |
| 40       | `relays[3]` |
| 44       | `status` (`DeviceStatus`) |
| 48       | `signal`    |

**v2, `relay_en=0`**: реле-блока нет, `relays=None` — значения на
смещениях 28–40 игнорируются (реальная плата кладёт туда `2`, эталонный
эмулятор — `0`). При этом `status` (44) и `signal` (48) плата и эталонный
эмулятор передают всегда: кадр длиной ≥ 52 байта декодируется с ними.
Кадр 28 байт (буква ПИВ: `relay_en=0` — «конец сообщения») тоже
принимается, тогда `status=None`, `signal=None`.

> Декодер length-tolerant: принимает и кадры переменной длины, и
> always-52-байтовые кадры. Неизвестная `version` → кадр отбрасывается.

### 3.4 `photos` — кадр фото (плата → backend)

Заголовок 20 байт (5 × uint32) + тело:

| Смещение | Поле          |
|----------|---------------|
| 0        | `device_id`   |
| 4        | `event_id`    |
| 8        | `photos_cnt`  |
| 12       | `photo_id`    |
| 16       | `photo_size`  |
| 20…      | тело фото (`photo_size` байт) |

Лимит `MAX_RELAY_PHOTO_BYTES` (env `MAX_MEDIA_TRANSFER_BYTES`, default
10 МиБ). Декодер (SEC-04 decode-bomb guard)
отвергает кадр, если `photo_size` превышает лимит **до** среза, и если
`len(raw) < 20 + photo_size` (усечённое тело).

> Каждый кадр — самостоятельный JPEG (начинается с `FFD8`; паддинг после
> `FFD9` допустим) и сразу публикуется как отдельное `media`. Кадры
> `(device_id, event_id)` в пределах эпохи `RELAY_PHOTO_SERIES_EPOCH_S`
> (default 24 ч) образуют серию в реестре `relay_photo_series`; первый кадр
> серии порождает `intrusion` security event. Повтор кадра распознаётся по
> sha256 тела без хвостового паддинга и не публикуется. Другой кадр на уже
> занятом `photo_id` или кадр к серии старше эпохи означает, что плата
> переиспользовала `event_id`: начинается новая серия и новый инцидент.
> Кадр привязывается к событию в history_api по `security_event_uid` (event_uid
> intrusion-события серии), независимо от порядка доставки. `photo_id` и
> `photos_cnt` вне 1..16 или тело без `FFD8` — malformed.

---

## 4. Поток команд

**Маппинг `command_type` → `Command`** (`command_type_to_relay`):

| `command_type` | `Command`         |
|----------------|-------------------|
| `OPEN`         | `LOCK_OPEN` (5)   |
| `CLOSE`        | `LOCK_CLOSE` (6)  |
| `REBOOT`       | `RELAY_REBOOT` (3)|

Команда без маппинга → `ValueError`. `SEND_STATUS` (4) эмитится
напрямую из `ingest`, не через маппинг.

**Аллокация `relay_cmd_id`** (`allocate_relay_cmd_id`): per-locker
монотонный `uint32` (`MAX(relay_cmd_id)+1` в пределах локера, перенос на
1 при достижении `4294967295`). Идемпотентна: если у команды уже есть
`relay_cmd_id` (повтор после ack-timeout), переиспользуется существующее
значение — иначе запоздавший `rep` со старым id не разрезолвится.
Назначение НЕ сериализовано (inline-доставка и queue-processor могут
аллоцировать конкурентно), поэтому unique-violation на индексе
`ux_locker_commands_relay_cmd_id` ловится и значение пере-вычисляется
(до `_RELAY_CMD_ID_MAX_RETRIES` попыток).

**Публикация** (`MqttTransport.send_command`): кодирует `com` через
`encode_com(cmd_id, command, data)` и публикует в `com/{relay_id}` с
QoS 1. Флаг retain выставляется, если команда входит в
`_RETAINED_COMMANDS = {LOCK_OPEN, LOCK_CLOSE, RELAY_ON, RELAY_OFF}`:
команды состояния retain'ятся, чтобы переподключившаяся плата перечитала
последнее желаемое состояние из `com/{id}`. One-shot/query команды
(`RELAY_REBOOT`, `SEND_STATUS`) НЕ retain'ятся — иначе плата при
реконнекте повторно выполнит устаревший reboot / переотправит статус.

**Матчинг `rep` → команда** (`RelayCommandResults.handle_rep`):

1. `resolve_relay_cmd_id(locker_id, cmd_id)` → `command_id` (без фильтра по
   статусу). Нет совпадения → WARNING, кадр отбрасывается (`outcome=processed`).
2. CAS-переход только из `pending`/`sent`/`delivered`: `SUCCESS` → `executed`
   (history-переход; для `OPEN`/`CLOSE` — состояние двери и persistent-уведомление
   «Шкаф открыт»), `FAILURE` / `NOT_SUPPORTED` → `failed` с причиной
   `failed` / `not_supported`. Принятый `rep` логируется на INFO (`rep accepted`).
3. Промах CAS (команда уже терминальна) → INFO `stale rep ignored`,
   `scud_mqtt_relay_messages_total{outcome="stale_rep"}`, статус, таймстампы,
   `result`, дверь и уведомление «Шкаф открыт» не меняются.
4. `SUCCESS` по `failed`-команде (`late_kind=after_failure`) или повтор `SUCCESS`
   позже TTL после `executed` (`late_kind=replay`, кроме `CLOSE`: retained
   `CLOSE` штатно переисполняется при переподписке платы и остаётся тихим
   `stale_rep`) → событие безопасности
   `command_late_execution` (severity `high`) с детерминированным `event_uid`
   (uuid5 от команды, вида опоздания и минутного окна приёма — дубли одного `rep`
   history_api схлопывает) и одно постоянное уведомление операторам и
   администраторам (`event=command_late_execution`).

`locker_id` передаётся в репозиторий как guard от cross-locker-подмены:
при несовпадении локера обновление статуса не происходит.

**Срок жизни команды (TTL).** `COMMAND_TTL_S` (по умолчанию 60 с) отсчитывается
от `created_at`. Просроченные `pending`/`sent`/`delivered` команды выметаются на
каждом тике queue-processor'а и батчами при старте сервиса: один SQL-statement
переводит их в `failed` («истёк срок ожидания ответа шкафа») и в той же транзакции
ставит outbox-событие `command_history`. Выборка и claim игнорируют просроченные
команды, поэтому после TTL команда на плату не отправляется. Пока сессия
locker_api с брокером оборвана, `send_command` возвращает `False`, не публикуя
(gmqtt иначе молча копит QoS1-пакет до реконнекта). Пакеты, накопленные в
момент обрыва, после реконнекта не переотправляются: сессия создаётся без
Session Expiry Interval, брокер отвечает `session_present=0`, и gmqtt
сбрасывает очередь. Неудачная отправка
возвращает claim в `pending` для повтора в пределах TTL. ack-timeout
(`COMMAND_ACK_TIMEOUT_MINUTES`) при дефолтах — лишь страховка: TTL срабатывает
раньше; при `COMMAND_TTL_S` больше ack-timeout сервис пишет WARNING на старте.
Ограничение: retained `com` не очищается, поэтому переподключившаяся плата
может выполнить уже просроченный `OPEN` — это фиксируется как
`command_late_execution`, но дверь при этом открывается.

---

## 5. Детекция offline

- **Inline (`stat=0`)**: `stat` с `status=OFFLINE` (0) → атомарный
  `mark_offline_if_online(locker_id)`; при флипе снимается транспорт и
  эмитится offline history event. Идемпотентно на повторных/retained
  кадрах `stat=0`.
- **Фоновый монитор** (`offline_monitor.py`): плата считается offline,
  если по ней не было `stat` дольше `MQTT_OFFLINE_TIMEOUT_S` (default
  350 с). Свип раз в `MQTT_OFFLINE_CHECK_INTERVAL_S` (default 30 с) делает
  один атомарный `UPDATE … WHERE transport='mqtt' AND connectivity='online'
  … RETURNING id`; для каждого флипнутого локера снимает транспорт и
  эмитит offline event (`reason=heartbeat_timeout`).

Монитор трогает только строки `transport='mqtt'` — значение `transport='ws'`
сохраняется в БД лишь как исторический маркер для уже неактивных записей и
живых подключений не имеет.

---

## 6. Provisioning и ACL

При регистрации релейной платы `locker_api` дописывает в конфиг брокера
(`/mosquitto/config`, том `mqtt_config`) учётку и ACL:

- **Логин**: `relay-{relay_id}`.
- **Пароль**: `sha256("relay-{relay_id}")[:16]` в hex (32 hex-символа).
  Выводится из публичного `relay_id` — **не секрет** (Phase 1; для
  production требуются TLS + per-device случайный пароль).
- **ACL-блок** на плату:

  ```
  user relay-{id}
  topic write stat
  topic write rep
  topic write photos
  topic read com/{relay_id}
  ```

  Плата может **писать** общие `stat`/`rep`/`photos` и **читать только**
  свой `com/{id}`.

Provisioning идемпотентен и выполняется под эксклюзивным `flock`. ACL-блок
пишется ДО строки пароля, чтобы учётка не существовала без своего
read-ACL. Watcher в контейнере брокера хеширует изменения и шлёт `SIGHUP`
для reload (~2 с).

Backend-учётка `locker_api` — `scud_backend`, пароль из Docker secret
`mqtt_password`. Подробности развёртывания брокера — в
`backend/DEPLOYMENT.md` → «MQTT-брокер (Mosquitto)».

---

## 7. Ingest-диспатч

`handle_message(topic, payload)` маршрутизирует входящие кадры по топику:
`stat` → декод+обработка статуса/телеметрии, `rep` → матчинг результата
команды, `photos` → `_handle_photo` (реестр серий, публикация кадра в
`media`). Кадры `photos` проходят отдельной полосой bulkhead с бюджетом байт
(`MQTT_INGEST_PHOTO_MAX_*`) и не занимают слоты `stat`/`rep`. Malformed/forged кадры (`ValueError`/
`KeyError`) логируются по длине, **без тела payload**.

Поля `stat` пишутся в датчики шкафа (`sensors.current_value`) и в history_api
отдельными кодами `sensor_type` (seed в `001_init.sql`). Датчик лениво
резолвится/создаётся по `(locker_id, code)` с серийным номером `relay-<code>`.
`current_value` хранит сырое значение протокола; подпись для UI отдаётся в поле
`display` датчика (см. `Sensor.display` в `openapi.yaml`).

| code       | Источник в `stat`        | value_type | Сырые значения            | `display` |
|------------|--------------------------|------------|---------------------------|-----------|
| `battery`  | `battery`                | numeric    | уровень заряда            | null |
| `signal`   | `signal`                 | numeric    | уровень сигнала           | null |
| `door`     | `door` (v2)              | boolean    | true — `OPEN`, иначе false | Открыта / Закрыта |
| `power`    | `power` (v2)             | numeric    | 0 — батарея, 1 — сеть     | От батареи / От сети |
| `net_type` | `net_type` (v2)          | numeric    | 0, 1, 2                   | Не определён / Ethernet / GSM |
| `error`    | `stat` (`status == 2`)   | boolean    | true — плата в `ERROR`    | Ошибка / Норма |
| `intrusion`| переход `door` (v2)      | boolean    | true — открыта без команды | Взлом / Норма |

Значение вне словаря отдаётся как «Неизвестное значение (N)» и не роняет кадр.

`battery`, `signal`, `power` и `net_type` — периодическая телеметрия:
не пишутся в `MAINTENANCE` и ограничиваются per-device rate-limit. `error`,
`door` и `intrusion` — состояние платы: значение пишется до этих гейтов
(запись в history у `door` остаётся под ними). `error` пишется только при
изменении; при `stat=0` (offline) сохраняет последнее значение, кадр без
`stat` (28 байт) его не трогает. Переход в `ERROR` дополнительно порождает
security-событие `threshold_violation` с `sensor_id` датчика `error`.

Подписи `display` берутся из `sensor_types.states` (seed-блок
`sensor-type-states` в `001_init.sql`).

Состояние двери (v2) дополнительно синхронизирует `lockers.door`:
`DoorStateRaw.OPEN/CLOSED` → `DoorState.OPEN/CLOSED`. `stat`, пришедший раньше
уже применённого, дверь не трогает.

Несанкционированное открытие (§7.1) распознаёт сервер: датчик `door` сменился с
закрыто на открыто, а последняя опубликованная в `com/{relay_id}` до этого `stat`
команда замка — не OPEN (или её нет). Команды, отвергнутые платой (`rep`
FAILURE / NOT_SUPPORTED, в том числе после истечения TTL), не считаются. Итог —
security-событие `intrusion` (high) и `intrusion` = true; закрытие двери
сбрасывает датчик в false. Шкаф в `normal` переводится в `alarm` с постоянным
уведомлением операторам; в `maintenance` и уже в `alarm` — только событие.
Первое наблюдение двери тревогу не поднимает.

После `rep` от платы, помеченной offline в БД, backend переспрашивает её
статус командой `SEND_STATUS` (`command=4`, `cmd_id=0`, retain=False) в
`com/{relay_id}` — `rep` на неё не ожидается.
