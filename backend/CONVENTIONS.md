# Конвенции API

## Именование URL-путей

Все REST API эндпоинты следуют конвенции единственного/множественного числа:

| Паттерн                                      | Путь                        | Пример                                                       |
|----------------------------------------------|-----------------------------|--------------------------------------------------------------|
| Коллекция (список, создание)                 | `/{plural}`                 | `GET /lockers`, `POST /users`                                |
| Пакетный запрос                              | `/{plural}/batch`           | `POST /lockers/batch`, `POST /accesses/batch`                |
| Экземпляр (получение, обновление, удаление)  | `/{singular}/{id}`          | `GET /locker/{uuid}`, `PATCH /user/{id}`                     |
| Вложенный ресурс                             | `/{singular}/{id}/...`      | `GET /locker/{uuid}/sensors`, `POST /user/{id}/accesses`     |
| Действие                                     | `/{singular}/{id}/{action}` | `POST /user/{id}/reactivate`, `POST /access/{id}/reactivate` |

### Примеры

```
# Коллекции (множественное число)
GET    /api/v1/lockers              # Список шкафов
POST   /api/v1/lockers              # Создать шкаф
POST   /api/v1/lockers/batch        # Пакетное получение шкафов
GET    /api/v1/users                # Список пользователей
POST   /api/v1/users                # Создать пользователя
POST   /api/v1/accesses/batch       # Пакетное получение допусков по user_ids

# Экземпляры (единственное число)
GET    /api/v1/locker/{uuid}        # Получить шкаф
PUT    /api/v1/locker/{uuid}        # Обновить шкаф
DELETE /api/v1/locker/{uuid}        # Удалить шкаф
GET    /api/v1/user/{id}            # Получить пользователя
PATCH  /api/v1/user/{id}            # Обновить пользователя
DELETE /api/v1/user/{id}            # Деактивировать пользователя
GET    /api/v1/access/{id}          # Получить допуск
DELETE /api/v1/access/{id}          # Деактивировать допуск

# Вложенные ресурсы (родитель в единственном числе)
GET    /api/v1/locker/{uuid}/sensors
POST   /api/v1/locker/{uuid}/commands
GET    /api/v1/user/{id}/accesses
POST   /api/v1/user/{id}/accesses

# Действия (родитель в единственном числе + глагол)
POST   /api/v1/user/{id}/reactivate    # Реактивировать деактивированного пользователя
POST   /api/v1/access/{id}/reactivate  # Реактивировать деактивированный допуск
```

### Обоснование

Конвенция устраняет конфликты путей между пакетными эндпоинтами (`/{plural}/batch`) и эндпоинтами экземпляров (`/{singular}/{id}`), поскольку они находятся под разными базовыми путями. OpenAPI-рендереры (ReDoc, Swagger UI) отображают все эндпоинты без неоднозначности.

---

## Семантика HTTP-методов

| Метод    | Семантика                                                                                                                                                        |
|----------|------------------------------------------------------------------------------------------------------------------------------------------------------------------|
| `GET`    | Только чтение, без побочных эффектов                                                                                                                             |
| `POST`   | Создание ресурса или вызов действия                                                                                                                              |
| `PUT`    | Полная замена ресурса                                                                                                                                            |
| `PATCH`  | Частичное обновление ресурса                                                                                                                                     |
| `DELETE` | **Мягкая деактивация** (не физическое удаление): устанавливает `state='deactivated'`/`state='archived'` или `is_active=false`. Ответ: `204 No Content`. |

`DELETE` никогда не удаляет записи физически. Для отмены деактивации используется `POST /{singular}/{id}/reactivate`.

> **Несоответствие текущей реализации (legacy).** На момент написания этой
> конвенции часть update-эндпоинтов использует `PUT` с фактически частичной
> семантикой (semantic-PATCH). Фактическое распределение по сервисам:
>
> | Сервис / handler                              | HTTP-метод | Семантика реализации |
> |-----------------------------------------------|------------|----------------------|
> | `user_api` — `/user/{id}`                     | `PATCH`    | частичная (корректно) |
> | `user_api` — `/access/{id}`                   | `PATCH`    | частичная (корректно) |
> | `user_api` — `/contractor/{id}`               | `PUT`      | частичная (semantic-PATCH) |
> | `locker_api` — `/locker/{uuid}`               | `PUT`      | частичная (semantic-PATCH) |
> | `locker_api` — `/locker/{uuid}/state`         | `PUT`      | частичная (semantic-PATCH) |
> | `locker_api` — `/locker/{uuid}/sensors/{id}`  | `PATCH`    | частичная (корректно) |
> | `locker_api` — `/locker/{uuid}/sensors/{sid}/thresholds/{tid}` | `PUT` | частичная (semantic-PATCH) |
> | `locker_api` — `/sensor-type/{id}`            | `PUT`      | частичная (semantic-PATCH) |
> | `history_api` — `/event/security/{id}`        | `PATCH`    | частичная (корректно) |
>
> Для **новых** update-эндпоинтов используйте `PATCH` — это соответствует
> реальной семантике (опциональные поля в теле запроса). `PUT` зарезервирован
> для будущей реализации полной замены ресурса. Существующие `PUT`-эндпоинты
> с частичной семантикой подлежат миграции на `PATCH` отдельным code-fix'ом.

---

## Пагинированные list-ответы

Все list-эндпоинты (`GET /<plural>`, `GET /<singular>/{id}/<children>`) — **публичные и `/internal/api/v1/*`** — возвращают единый envelope:

```json
{
  "items": [...],
  "total": 542,
  "limit": 100,
  "offset": 0
}
```

- `items` — записи текущей страницы.
- `total` — общее число записей с применёнными фильтрами (без учёта пагинации).
- `limit`, `offset` — echo введённых query-параметров (или их default'ов).

**Важно**: фильтры (`state`, `locker_id`, `severity`, …) **не** эхо-ятся в ответе — клиент уже знает, что отправил.

### Реализация

1. Импортировать общий dataclass:
   ```python
   from shared.dtos import PaginatedResponse
   ```
   Это единственный источник истины (`backend/shared/dtos.py`). Не дублировать локально.

2. Парсинг query-параметров — через хелпер базового класса:
   ```python
   params = self.parse_pagination_params(default_limit=100, max_limit=1000)
   if params is None:
       return  # 400 уже отправлен
   limit, offset = params
   ```

3. Репозиторий обязан предоставить **парный** метод `count*()` рядом с `get_all()`/`get_by_filters()`:
   ```python
   async def count(self, ...) -> int: ...
   async def get_all(self, ..., limit: int = 100, offset: int = 0) -> list[Model]: ...
   ```

4. Handler собирает ответ:
   ```python
   self.write_json(
       PaginatedResponse(items=..., total=..., limit=limit, offset=offset)
   )
   ```

### Стандартные пределы limit

| Тип ресурса                          | DEFAULT | MAX     |
|--------------------------------------|---------|---------|
| CRUD-сущности (users, lockers, …)    | 100     | 1000    |
| Тяжёлые timeseries (telemetry, security_events, command_history, analytics, photos) | 1000 | 10000 |

### Эталоны

- `services/history_api/app/handlers/security_events.py:21-102` — самая чистая реализация.
- `services/locker_api/app/handlers/locker.py:20-55` — для locker_api consistency.

## Подстрочный поиск (`q`)

List-эндпоинты с текстовым поиском используют **единый** query-параметр `q`
(case-insensitive подстрока через `ILIKE`). Не вводить альтернативные имена
(`search`, `name`, …) — только `q`. Вся логика — в общем модуле
`backend/shared/search.py`; не дублировать экранирование/нормализацию по сервисам.

**Семантика `q`:**

- значение обрезается по краям (`strip`); пустая строка/только пробелы →
  фильтр не применяется (как будто параметр не передан);
- LIKE-метасимволы `%`, `_` и сам экранирующий `\` экранируются и ищутся как
  **литералы** (`q=50%` находит буквальный знак процента, а не «всё»);
- превышение `max_length` → `400` `ProblemDetails` со ссылкой на поле `q`;
- `total` в ответе учитывает тот же фильтр, что и `items`;
- поиск может идти по нескольким колонкам через `OR` (один параметр — оба поля).

### Реализация

1. **Handler** — нормализация значения:
   ```python
   from shared.search import normalize_query

   _Q_MAX_LENGTH = 255  # = длина искомой VARCHAR-колонки

   q_raw = self.get_query_argument("q", default=None)
   try:
       q = normalize_query(q_raw, max_length=_Q_MAX_LENGTH)
   except ValueError as exc:
       self.write_validation_error(str(exc))
       return
   ```
   `q` (уже trimmed либо `None`) прокидывается в **парные** методы
   `get_all(q=...)` и `count(q=...)`.

2. **Repository** — сборка SQL-фрагмента:
   ```python
   from shared.search import like_filter

   if q:
       clause, qps = like_filter(q, "inventory_number", "location_address")
       query += f" AND {clause}"
       params.extend(qps)
   ```
   `like_filter` возвращает фрагмент `(<col> ILIKE %s ESCAPE '\' OR ...)`
   (без ведущего `AND`) и список уже экранированных `%…%`-паттернов по числу
   колонок.

3. **OpenAPI** — параметр `q` (`type: string`, `maxLength` = длине колонки) с
   описанием семантики выше. Эталон формулировки —
   `services/locker_api/openapi/components/parameters.yaml` (`SearchQuery`).

### Эталон

- `services/locker_api/app/handlers/locker.py` + `.../repositories/locker.py` —
  поиск по двум колонкам через `shared.search`.
- `services/user_api/app/handlers/user.py:147-198` — для user_api consistency.

### OpenAPI

Каждая list-схема в `services/<svc>/openapi/schemas/*.yaml` объявляется как:
```yaml
<Resource>ListResponse:
  type: object
  required: [items, total, limit, offset]
  properties:
    items: { type: array, items: { $ref: '#/<Resource>' } }
    total: { type: integer }
    limit: { type: integer }
    offset: { type: integer }
```

Параметры `limit`/`offset` — из `components/parameters.yaml#/LimitQuery`/`OffsetQuery`.

---

## Soft-delete: где какой маркер использовать

В системе есть три варианта soft-delete — выбор зависит от того, что уже есть в схеме таблицы. Все читающие методы репозитория обязаны фильтровать «активные» записи.

| Таблица                 | Маркер soft-delete            | Read-фильтр                       | Источник правды |
|-------------------------|-------------------------------|-----------------------------------|-----------------|
| `users`                 | `state` enum (`active`/`deactivated`/`deleted`) | `state <> 'deleted'` (или `state = 'active'`) | `user_api/db/migrations` |
| `lockers`               | `state` (`active`/`archived`) | `state = 'active'`                | `locker_api/db/migrations` |
| `contractors`           | `state` (`active`/`archived`) | `state = 'active'`                | `user_api/db/migrations` |
| `sensors`               | `is_active` bool              | `is_active = true`                | `locker_api/db/migrations` |
| `sensor_thresholds`     | `is_active` bool              | `is_active = true`                | `locker_api/db/migrations` |
| `user_accesses`         | `is_active` bool              | `is_active = true`                | `user_api/db/migrations` |
| `locker_media`          | `deleted_at` timestamp        | `deleted_at IS NULL`              | `history_api/db/migrations` |

### Ephemeral / технические таблицы — оправданный hard-delete

Эти таблицы НЕ требуют soft-delete: запись по природе одноразовая или короткоживущая.

| Таблица                | Почему hard-delete нормален                                                                |
|------------------------|--------------------------------------------------------------------------------------------|
| `access_tokens`        | Ротируемые сессионные токены; `revoke()` уже ставит `revoked_at`, retention чистит истёкшие |
| `otp_codes`            | Одноразовые коды; `mark_used()` ставит `used_at`, истёкшие удаляются по cron               |
| `security_events`, `command_history`, `telemetry_aggregations` | Очистка по retention-политике (24 месяца) |
| `user_access_lockers`  | Связь many-to-many; запись уходит вместе с soft-delete родительского `user_accesses`        |

### Правило при добавлении нового CRUD-репозитория

1. Если в таблице уже есть `state`/`is_active`/`deleted_at` — переиспользовать его.
2. Если нет, и сущность user-facing (создаётся/удаляется через REST API) — **добавить колонку**, не делать `DELETE FROM`.
3. Все методы `get_by_id`, `get_all`, `get_batch*`, `count` обязаны фильтровать по выбранному маркеру.
4. `delete()` использует `UPDATE ... RETURNING id` (через `fetch_one`), возвращает `bool` — был ли активный объект помечен удалённым.
5. Тех/ephemeral таблицы из второй таблицы выше — освобождены от правила, но это нужно прокомментировать в репозитории.

### Особый случай: `emergency_requests` — `DELETE` значит «закрыть», а не «скрыть»

`DELETE /api/v1/emergency-request/{id}` не прячет строку, а закрывает заявку:
`status` переходит `pending` → `closed`, проставляются `closed_at`/`closed_by`,
запись сохраняется. Читающие методы закрытые заявки НЕ фильтруют — они видны
через `GET /api/v1/emergency-requests?status=closed`. Поэтому таблицы нет в
списке soft-delete выше: `status` здесь описывает жизненный цикл
(открыта/закрыта), а не удаление. Закрыть может только operator/administrator;
повторный `DELETE` по закрытой заявке — `409`. Подробности — в
`services/user_api/README.md` и OpenAPI-спеке сервиса.

---

## Чек-лист: добавление и изменение эндпоинтов

При добавлении нового эндпоинта или изменении существующего пройди каждый пункт.

### Новый эндпоинт

- [ ] **Handler** — добавить класс/метод обработчика в `app/handlers/*.py`
- [ ] **Маршрут** — зарегистрировать в `app/main.py`; более специфичные паттерны (например, `/reactivate`) должны стоять **до** общих (например, `([^/]+)`)
- [ ] **Правило ACM** — добавить `Rule(method, pattern, Role.*)` в `services/acm/app/rules.py`; отсутствующее правило = 403 (запрет по умолчанию)
- [ ] **Шлюз** — убедиться, что в `gateway/apache.conf` есть `ProxyPass`, покрывающий новый префикс пути; добавить при необходимости
- [ ] **OpenAPI путь** — добавить операцию в соответствующий файл `openapi/paths/*.yaml`
- [ ] **OpenAPI корень** — зарегистрировать путь в `openapi/openapi.yaml`
- [ ] **Интеграционный тест** — покрыть happy path, граничные случаи 404/400 и 401 (отсутствует `X-Remote-User`)

### Изменение существующего эндпоинта

- [ ] **Изменение сигнатуры** (параметры пути, метод) — обновить маршрут в `main.py` и regex правила ACM
- [ ] **Изменение поведения** — обновить обработчик и репозиторий; обновить описание/ответы в OpenAPI
- [ ] **Изменение уровня доступа** — обновить `Role.*` в правиле ACM
- [ ] **Тесты** — обновить или добавить интеграционные тесты, отражающие новое поведение

---

## Inter-service HTTP

Все вызовы между бэкенд-сервисами **обязаны** идти через `shared.http.RetryHTTPClient`
(`backend/shared/http/retry_client.py`). Прямое использование `tornado.httpclient.AsyncHTTPClient`
запрещено — оно не ретраится и при первом сбое 5xx/network теряет данные.

`RetryHTTPClient`:
- 3 попытки с экспоненциальным backoff (0.2 → 0.4 → 0.8 секунды);
- 5xx и сетевые ошибки ретраятся, **4xx — нет** (это ошибка вызова, не временный сбой);
- логирует каждую неудавшуюся попытку (`WARNING`) и пробрасывает последнее исключение, если все попытки исчерпаны.

**Каноничный паттерн** — обернуть вызовы в специализированный клиент. Эталоны:

- `backend/services/locker_api/app/services/notification_client.py` — `NotificationClient` к notification_api.
- `backend/services/auth_api/app/services/user_api_client.py` — `UserApiClient` к user_api с явным разделением:
  - 4xx 401/404 → `None` (legitimate "не найдено / неверно");
  - исчерпанные retry или неожиданный 4xx → `UserApiUnavailableError` (хендлер должен ответить 503);
  - сам клиент инстансируется один раз при старте сервиса и кладётся в `application.settings`.

**RFC 7807 Problem Details** — все error-ответы используют `ProblemType.*` / `ProblemTitle.*`
из `shared.http.problem`. Не использовать `"about:blank"` или bare-строки заголовков
(`"Bad Request"`, `"Not Found"`). При недоступности upstream — `ProblemType.SERVICE_UNAVAILABLE` (503).

**HMAC-подписи между сервисами: per-domain keys.** Эндпоинты под
`/internal/*` подписываются HMAC-SHA256 со специализированными общими
ключами. **Принцип**: один канал — один ключ; компрометация одного
не каскадирует в другие. Канон подписи (SEC-20) привязан к методу и пути:
`timestamp + METHOD + path + sha256(body)` через
`shared.utils.compute_request_signature` — единый формат для всех каналов
(подпись нельзя переиграть на другой endpoint). Существующие ключи:

- `INTERNAL_AUTH_HMAC_KEY` — `auth_api ↔ user_api /internal/auth/check`
  (телефон/пароль уходят в `sha256(body)`, в канон не попадают).
- `INTERNAL_OTP_HMAC_KEY` — `auth_api ↔ notification_api /internal/api/v1/otp/send`.
- `INTERNAL_MAX_BINDING_HMAC_KEY` — `notification_api ↔ user_api /internal/user/by-invite`.
- `INTERNAL_STEP_UP_HMAC_KEY` — step-up 2FA flow для команд шкафа: подписывает
  `auth_api ↔ user_api /internal/user/{id}/max-context` (issue) **и**
  `locker_api ↔ auth_api /internal/api/v1/step-up/{issue,verify}`. Один ключ
  на оба канала, потому что это один логический домен (issue+verify пара
  всегда исполняется вместе).

Для любого нового подписанного inter-service endpoint используйте
`compute_request_signature` / `verify_request_signature` из `shared.utils`
и заведите отдельный ключ через `read_secret(..., env_var=...)`. Окно
валидности — 30 секунд (`DEFAULT_INTERNAL_SIGNATURE_MAX_AGE_SECONDS`).

---

## База данных: single-statement транзакции (инвариант PgBouncer)

Перед каждым из 6 PostgreSQL-инстансов стоит PgBouncer в режиме
`pool_mode=transaction` (по одному пулеру на инстанс,
`pgbouncer_<instance>:6432`; см. `backend/pgbouncer/README.md`). Transaction-режим
возвращает серверное соединение в пул **на границе каждой транзакции**, поэтому
любое состояние, переживающее границу транзакции на одном соединении, ломается
тихо (другой клиент получит то же соединение уже без этого состояния).

**Инвариант (обязателен для всего нового кода):** каждая БД-операция остаётся
**single-statement** — один `execute` = одна транзакция = один checkout
соединения. `AsyncPostgres` (`execute` / `fetch_one` / `fetch_all`) уже
устроен так: `BEGIN → один statement → COMMIT`. Категорически нельзя вводить:

- multi-statement транзакции, удерживаемые между несколькими `await`;
- session-level prepared statements (server-side `PREPARE`);
- advisory locks (`pg_advisory_lock`), `LISTEN`/`NOTIFY`;
- session-level `SET` (только `SET LOCAL` внутри одной транзакции допустим).

Outbox-диспетчер (`OutboundDispatcher`) уже следует правилу: заявка строк —
один `UPDATE ... RETURNING` поверх `FOR UPDATE SKIP LOCKED`, без удержания
блокировки между запросами. Если новой фиче нужна многооператорная транзакция —
это сигнал пересмотреть дизайн, а не отключать пулинг. Подробности и тюнинг
пула — `backend/DEPLOYMENT.md → "PgBouncer (transaction pooling)"`.

---

## Изменения схемы для существующих БД

`001_init.sql` выполняется только на пустой БД. Отдельных файлов миграций
(`002_*.sql`) нет. Любое изменение, которое должно дойти до уже развёрнутых БД
(колонка, таблица, индекс, значение enum, правка данных), оформляется
**именованным блоком** в том же `001_init.sql` — на месте объекта, там, где он
определён для свежей БД:

```sql
-- relay-control:begin
ALTER TABLE lockers ADD COLUMN IF NOT EXISTS has_relays BOOLEAN;
-- relay-control:end
```

Правила:

- Маркеры — отдельные строки ровно вида `-- <имя>:begin` / `-- <имя>:end`;
  имя из `[a-z0-9-]`, уникально в файле; блоки не вкладываются, `end`
  совпадает с `begin`. Нарушение разметки — ошибка `deploy.sh`.
- Блок **идемпотентен**: `IF NOT EXISTS`, `ADD VALUE IF NOT EXISTS`,
  `DO`-проверки, `UPDATE` с условием. Он должен давать одну и ту же схему и на
  свежей БД сразу после инициализации, и на старой, и при повторном запуске.
- Не полагаться на оборачивающую транзакцию: `deploy.sh` выполняет блок через
  `psql -v ON_ERROR_STOP=1` **без `-1`**, каждый оператор фиксируется
  отдельно (иначе значение enum из `ALTER TYPE … ADD VALUE` нельзя было бы
  использовать в том же блоке). Прерванный блок повторяют целиком.
- Разрушающие изменения (`DROP`, смена типа, потеря данных) идемпотентным
  блоком не выражаются без отдельного решения — опишите порядок выкатки и
  откат в `backend/DEPLOYMENT.md` («Обновление существующего развёртывания»).

Как `deploy.sh` обращается с блоками. В каждой БД ведётся таблица
`deploy_schema_upgrades(name, checksum, applied_at)`; `checksum` — sha256
текста блока от строки `begin` до `end` включительно (без `\r`), поэтому
**любая** правка внутри блока, даже комментария или пробела, меняет сумму:

| Состояние | Действие |
|---|---|
| имя и сумма есть в журнале | пропуск |
| имени нет | выполнить, записать имя и сумму |
| имя есть, сумма другая | выполнить заново, обновить запись (в логе `~ изменился`) |

Отсюда правило: **выпущенный блок неизменяем.** Повторное выполнение
изменённого блока не отменяет того, что сделала его прежняя версия (например,
не удалит добавленную колонку). Новое изменение — новый блок с новым именем. Правка
уже выпущенного блока допустима, только если вы сознательно хотите, чтобы он
выполнился заново на всех БД (например, исправить неидемпотентный оператор).
Свежая БД при `install` получает все блоки в журнал как «применённые» без
выполнения — её уже создал `001_init.sql`.

Новый блок сопровождают:

1. строка в таблице блоков `backend/DEPLOYMENT.md` («Обновление существующего
   развёртывания»), а для нетривиального порядка выкатки или отката — пункт
   рядом с ней;
2. запуск `bash backend/scripts/tests/test_schema_upgrade.sh` (Docker,
   `postgres:15-alpine`; шаг CI `schema upgrade blocks`): для каждой из 6 БД
   сверяет схему свежей инициализации со схемой «инициализация + блоки,
   применённые дважды» и с БД, созданной ревизией `cc41adc` плюс блоки.
   Тест должен проходить; `sqlfluff` (см. «SQL стиль миграций») — тоже.

---

## Хеширование паролей

Пароль пользователя хранится в `scud_users.user_auth.password_hash` как
**`bcrypt(sha256(plaintext))`** — два слоя:

1. `shared.utils.hash_password` — SHA-256-хэш на **стороне клиента/транспорта**
   (имя историческое: это пред-хэш, передаваемый по сети, а **не** финальное
   хранилище). Plaintext-пароль в открытом виде между слоями не ходит.
2. Сервер дополнительно оборачивает полученный SHA-256 в bcrypt
   (`user_api` → `bcrypt.hashpw`), проверка — `bcrypt.checkpw`
   (`user_api/app/handlers/internal_auth.py`).

Функцию `hash_password` **не переименовываем** (сломает совместимость
транспорта). Для нового кода: не путайте transport-слой (SHA-256) с финальным
storage-слоем (bcrypt поверх него) — в БД всегда лежит bcrypt-хэш.

---

## Enum-классы для бизнес-констант

Соглашения проекта запрещают bare-литералы (`"active"`, `"administrator"`, `"open"`, …) для
бизнес-констант. Правило применения:

| Где живёт enum                                                         | Когда использовать                                                                  |
|------------------------------------------------------------------------|-------------------------------------------------------------------------------------|
| `backend/shared/enums.py`                                              | Cross-service бизнес-константы — известны более чем одному сервису                  |
| `backend/services/<svc>/app/<svc>_data/db/models/*.py`                 | Intra-service — нужны только своему сервису                                          |

**Cross-service enum'ы** (в `shared.enums`):
- `UserState` (`active`/`deactivated`/`deleted`) — user_api, acm, auth_api
- `UserRole` (`worker`/`operator`/`administrator`) — user_api, auth_api, locker_api
- `SecurityEventType` / `EventSeverity` — контракт locker_api → history_api `/internal/events/security`

**Intra-service enum'ы** (остаются в моделях своего сервиса):
- `DoorState`, `Connectivity`, `OperationalStatus`, `LockerView` — `locker_api/models/locker.py`
- `NotificationType` — `notification_api/models/notification.py`
- `CommandStatus` — `locker_api/models/command.py` и `history_api/models/command_history.py`
- `SensorValueType`, `SensorCode` — `locker_api/models/sensor_type.py`
- `SensorDisplaySeverity` — `locker_api/models/sensor.py`

**Перечислимые значения датчиков.** `sensor_types.value_type` — тип хранения. В
`sensors.current_value` пишется только сырое значение протокола (`0/1/2`,
`true/false`), никогда не строка-подпись. Подписи для показа хранятся в
`sensor_types.states` (JSONB) и отдаются полем `display` датчика.

**SQL-литералы** также нужно параметризовать через enum — `WHERE state = %s` с параметром
`UserState.ACTIVE.value`, не `WHERE state = 'active'`. dacite-репозитории, читающие enum-поля
из БД, должны добавить `Config(cast=[<EnumClass>])`, иначе строка не сконвертируется в enum.

---

## Зависимости и `requirements.txt`

Все Python-пакеты ставятся через `apt install python3-<pkg>` в Dockerfile сервиса
(Astra-репозитории — см. `available_packets.txt` в корне репо). Pip-этапа в Dockerfile нет,
runtime-зависимости приходят исключительно из системных apt-пакетов.

Файлы `services/<svc>/requirements.txt` существуют для целей **аудита и SBOM/SCA**:

- содержат **все** runtime-зависимости сервиса с версиями, совпадающими с apt-пакетами
  (мажор-минор-патч из `available_packets.txt`);
- при добавлении нового импорта security-критичного пакета (`cryptography`, `bcrypt`, `PyJWT`,
  и т.п.) обязательно зафиксировать его и в `Dockerfile` (через apt), и в `requirements.txt`;
- если apt-версия обновляется в Astra — синхронизировать пин в `requirements.txt`.

---

## Качество кода (линтинг и покрытие)

Боевой код в `backend/services/*/app/` и `backend/shared/` проходит через
CI job `lint` (`.github/workflows/tests.yml`). Конфиги — в корневом
`pyproject.toml`, `.hadolint.yaml`, `.luacheckrc`, `.shellcheckrc`.

| Инструмент | Что проверяет | Где применяется |
|---|---|---|
| ruff | Стиль, баги, секьюрити (max ruleset) | `backend/services` + `backend/shared`; per-file ignore для тестов |
| mypy --strict | Статическая типизация | то же, с baseline-overrides для `dacite`/`psycopg2`/`tornado`/`shared.database` |
| bandit | Security-патерны | то же |
| sqlfluff | Postgres SQL | `backend/services/*/app/*_data/db/migrations/` |
| hadolint | Dockerfile | все backend Dockerfile (multi-stage AS production + AS test) |
| shellcheck | bash | `backend/scripts/*.sh` |
| luacheck | Apache mod_lua | `backend/gateway/lua/` |

Линтеры — на CI runner (pip/apt/curl), не в Astra-сертифицированных
образах. Pytest — в Docker.

**Покрытие**: `*_tests` контейнеры запускают `pytest --cov` благодаря
test-stage Dockerfile. Данные —
`backend/.coverage-data/<svc>/` (gitignored), агрегируются в CI job
`coverage-combine`. Анти-регресс-порог `fail_under = 59%`
(`[tool.coverage.report]` в корневом `pyproject.toml`) — это измеренное
объединённое покрытие 64% минус 5 п.п. запаса.

Порог проверяется ТОЛЬКО на объединённом шаге job-а `coverage-combine`:
`coverage report` сравнивает суммарный процент по всем 8 источникам с
`fail_under` и завершается кодом 2, если покрытие просело ниже. Per-service
шаг в матрице `backend-tests` намеренно вызывает
`coverage report --fail-under=0` (диагностический вывод, не гейт) — иначе
любой отдельный сервис ниже среднего объединённого показателя ронял бы свой
шаг. При заметном росте покрытия порог поднимаем отдельным PR.

### SQL стиль миграций

Source of truth — `pyproject.toml [tool.sqlfluff.*]` + `[tool.sqlfluff.rules.*]`
(чтобы здесь не было рассинхрона). Ключевые тезисы для быстрой ориентации:
boolean/NULL литералы UPPERCASE, функции lowercase, типы/идентификаторы
смешанные (CP02/CP05 OFF), 120 символов max, бинарные операторы при переносе
ставим в начало новой строки.

При добавлении миграции проверять локально:
`sqlfluff lint backend/services --dialect postgres`.
