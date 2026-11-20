# Auth API

Сервис аутентификации системы СКУД. Управляет входом пользователей по телефону/паролю с опциональной двухфакторной аутентификацией (SMS OTP), выдаёт сессионные cookie и проверяет сессии для Apache Lua.

**Стек:** Python 3.11, Tornado 6.2.0, PostgreSQL 15
**Порт:** 8003
**База данных:** `scud_auth` (отдельный PostgreSQL, порт 5435)

## Место в системе

```
Браузер → Apache (80) → [mod_lua jwt_auth.lua]
                              ↓ GET /auth/validate (X-Access-Token)
                         Auth API (8003) ← ← ← ← ← ← ← ←
                              ↓ POST /internal/auth/check       ↑
                         User API (8000)          Бэкенды (8001+)
```

Все защищённые запросы проходят через Apache Lua, который:
1. Извлекает `access_token` из Cookie клиента
2. Вызывает `/auth/validate` → получает `X-User-Id`
3. Удаляет Cookie из запроса, добавляет `X-Remote-User: <user_id>`
4. Проксирует запрос в нужный бэкенд

---

## Структура проекта

```
auth_api/
├── app/
│   ├── main.py                          # Точка входа: конфиг, роутинг, cleanup loop
│   ├── handlers/
│   │   ├── base.py                      # BaseHandler, хелперы ответов (ProblemDetails)
│   │   ├── login.py                     # POST /api/v1/auth/login
│   │   ├── verify.py                    # POST /api/v1/auth/verify
│   │   ├── access.py                    # POST /api/v1/auth/access (ротация токена)
│   │   ├── logout.py                    # POST /api/v1/auth/logout
│   │   └── validate.py                  # GET /auth/validate (внутренний, для Apache)
│   ├── services/
│   │   └── user_api_client.py           # UserApiClient к user_api (RetryHTTPClient)
│   └── auth_data/db/
│       ├── migrations/001_init.sql      # Схема БД
│       ├── models/tokens.py             # OtpCode, AccessToken (dataclass)
│       └── repositories/
│           ├── token_repository.py      # CRUD для access_tokens
│           └── otp_repository.py        # CRUD для otp_codes
├── openapi/                             # OpenAPI 3.2.0 спецификация
├── tests/
│   ├── integration/                     # Интеграционные тесты (full stack)
│   └── unit/                            # Unit-тесты (моки RetryHTTPClient)
└── Dockerfile
```

---

## API эндпоинты

### POST `/api/v1/auth/login`

Шаг 1: проверка телефона и пароля.

**Тело запроса:**
```json
{ "phone": "+79001234567", "password": "changeme" }
```

**Ответ 200** — два варианта:

| `require_2fa` | Тело | Cookie |
|---|---|---|
| `false` (напр. администратор) | `{"message": "Authentication successful", "name": "...", "role": "..."}` | `Set-Cookie: access_token=...` |
| `true` | `{"message": "OTP has been sent"}` | — |

При `require_2fa=true` генерируется 6-значный OTP и отправляется по SMS (fallback — Max). Если код не доставлен ни одним каналом, ответ 503.

**Ошибки:**
- `400` — отсутствуют поля
- `401` — неверный телефон или пароль
- `429` — превышен rate limit (10 попыток/60 сек с одного IP)
- `503` — `user_api` недоступен (исчерпаны retry на network/5xx); ответ имеет
  `type: https://api.scuddocs.com/problems/service-unavailable`

---

### POST `/api/v1/auth/verify`

Шаг 2: проверка OTP из SMS (только при `require_2fa=true`).

**Тело запроса:**
```json
{ "phone": "+79001234567", "code": "123456" }
```

**Ответ 200:**
```json
{"message": "Authentication successful", "name": "Иванов Иван Иванович", "role": "administrator"}
```
Имя и роль возвращаются симметрично `/login` без 2FA — фронт после verify знает,
кто вошёл и куда маршрутизировать.

+ `Set-Cookie: access_token=<token>; HttpOnly; SameSite=Strict; Path=/; Max-Age=<ttl>`

**Ошибки:**
- `400` — отсутствуют поля
- `401` — неверный или истёкший OTP (увеличивает счётчик попыток)
- `429` — превышен лимит попыток (`OTP_MAX_ATTEMPTS`)

---

### POST `/api/v1/auth/access`

Ротация сессионного cookie. Старый токен отзывается, выдаётся новый.

**Запрос:** Cookie `access_token=<token>` (не нужно тело)

**Ответ 200:**
```json
{"message": "Session renewed"}
```
+ новый `Set-Cookie: access_token=<new_token>; ...`

**Ошибки:** `401` — нет cookie или токен недействителен

---

### POST `/api/v1/auth/logout`

Отзыв сессии. Идемпотентен (всегда возвращает 204).

**Запрос:** Cookie `access_token=<token>` (опционально)

**Ответ 204** + `Set-Cookie: access_token=; Max-Age=0` (очищает cookie)

---

### DELETE `/api/v1/auth/users/{user_id}/tokens` *(только администратор)*

Отзывает все активные сессии указанного пользователя.
Требует роли `administrator` у вызывающего (проверяется через User API).

**Запрос:** Cookie `access_token=<token>` + путевой параметр `user_id`

**Ответ 200:**
```json
{"revoked": 3}
```
`revoked` — количество отозванных токенов (0 если у пользователя не было активных сессий).

**Ошибки:**
- `400` — невалидный `user_id`
- `401` — нет активной сессии
- `403` — вызывающий не является администратором

---

### GET `/auth/validate` *(внутренний)*

Вызывается только Apache Lua. Снаружи заблокирован (`Require all denied` в apache.conf).

**Запрос:** Заголовок `X-Access-Token: <token>`

**Ответ 200:** Заголовок `X-User-Id: <user_id>`
**Ответ 401:** токен недействителен, истёк или отозван

---

## Step-up 2FA для команд

Auth API выпускает короткоживущие OTP-челленджи для «привилегированных»
команд на шкафу (`open`, `reboot`, `config`) когда у пользователя
`require_2fa=true`. Это **второй** OTP-канал в системе — независимый от
входного при `/auth/login`: каждая команда требует отдельного подтверждения,
challenge не переиспользуется и привязан к конкретному `locker_id`.

Триггер выставляется на стороне ACM: при роуте `POST /locker/{id}/commands`
ACM прокидывает в gateway заголовок `X-Remote-Require-2FA: true`, Apache Lua
форвардит его в locker_api, locker_api вызывает step-up issue.

### Внутренние эндпоинты

Оба закрыты снаружи (`<Location /internal> Require all denied` в apache.conf)
и подписаны HMAC по схеме `timestamp + sha256(body)` с ключом
`INTERNAL_STEP_UP_HMAC_KEY` (один и тот же ключ для locker_api → auth_api и
auth_api → user_api `/internal/user/{id}/max-context`).

**`POST /internal/api/v1/step-up/issue`** — выпускает challenge. Принимает
`{user_id, scope_kind, scope_payload, ttl_seconds}`. Генерирует 6-значный
OTP, кладёт SHA-256 хэш в `step_up_challenges` и просит notification_api
доставить plaintext (SMS, при неудаче — Max-бот). Возвращает 201 с
`{challenge_id, expires_at, delivery_status}`. `delivery_status =
max_binding_required` и `challenge_id = null` — только если доставить некуда
(нет ни телефона, ни привязки Max).

**`POST /internal/api/v1/step-up/verify`** — принимает `{challenge_id, user_id,
code}`. Атомарный `SELECT ... FOR UPDATE` гарантирует, что counters
`attempts` / `used_at` не гонятся; lockout наступает при `attempts >=
OTP_MAX_ATTEMPTS`. Возвращает 200 с `{scope_kind, scope_payload}` —
locker_api сверяет их с исходным запросом перед постановкой команды в очередь.

Полные схемы — `openapi/openapi.yaml` (раздел `/internal/api/v1/step-up/*`).

### Cleanup expired challenges

Фоновая задача в `main.py` (тот же `cleanup_loop`, что чистит истёкшие
access-токены и OTP) раз в час выполняет `DELETE FROM step_up_challenges
WHERE expires_at < now() OR used_at IS NOT NULL`. Лишние записи не растут
бесконечно в `scud_auth`.

### Security events

Auth API публикует в history_api события безопасности `command_2fa_issued`
(severity=info), `command_2fa_verified` (severity=info) и `command_2fa_failed`
(severity=warning) для аудита привилегированных операций. Канал —
`POST /internal/api/v1/events/security` в history_api
(см. `app/services/security_event_producer.py`). Событие `command_2fa_mismatch`
(severity=alert) публикуется зеркальным продьюсером в locker_api при
несовпадении scope.

---

## Поток аутентификации

### С двухфакторной аутентификацией

```
Клиент                    Auth API              User API
  │                          │                     │
  │── POST /login ──────────►│                     │
  │   {phone, password}      │── POST /internal ──►│
  │                          │   /auth/check        │
  │                          │◄── {user_id,         │
  │                          │    require_2fa:true}  │
  │                          │                     │
  │                          │  Генерирует OTP      │
  │                          │  Сохраняет хэш в БД  │
  │                          │  Отправляет SMS       │
  │◄── 200 "OTP has been ────│                     │
  │    been sent"            │                     │
  │                          │                     │
  │── POST /verify ─────────►│                     │
  │   {phone, code}          │  Проверяет хэш OTP   │
  │                          │  Помечает OTP        │
  │                          │  как использованный  │
  │                          │  Создаёт access token│
  │◄── 200 + Set-Cookie ─────│                     │
  │    access_token=...      │                     │
```

### Без двухфакторной аутентификации

```
Клиент                    Auth API              User API
  │                          │                     │
  │── POST /login ──────────►│── POST /internal ──►│
  │   {phone, password}      │   /auth/check        │
  │                          │◄── {user_id,         │
  │                          │    require_2fa:false} │
  │                          │  Создаёт access token│
  │◄── 200 + Set-Cookie ─────│                     │
  │    access_token=...      │                     │
```

### Авторизация запроса через Apache Lua

```
Браузер           Apache/Lua              Auth API         Бэкенд
  │                   │                      │                │
  │─ GET /api/v1/* ──►│                      │                │
  │  Cookie: token    │── GET /auth/ ────────►│               │
  │                   │   validate            │               │
  │                   │   X-Access-Token:     │               │
  │                   │   <token>             │               │
  │                   │◄── 200 X-User-Id:42 ──│               │
  │                   │                      │                │
  │                   │─ GET /api/v1/* ──────────────────────►│
  │                   │  X-Remote-User: 42                    │
  │                   │  (без Cookie)                         │
```

---

## Конфигурация

Все переменные читаются в `app/main.py`. При отсутствии используются дефолты.

### База данных

| Переменная | Дефолт | Описание |
|---|---|---|
| `DB_HOST` | `localhost` | Хост PostgreSQL (auth) |
| `DB_PORT` | `5432` | Порт PostgreSQL |
| `DB_USER` | `postgres` | Пользователь БД |
| `DB_PASSWORD` | `postgres` | Пароль БД |
| `DB_NAME` | `scud_auth` | Имя базы данных |

### Приложение

| Переменная | Дефолт | Описание |
|---|---|---|
| `APP_PORT` | `8003` | Порт HTTP-сервера |
| `USER_API_URL` | `http://user_api:8000` | URL User API (проверка пароля) |

### Безопасность

| Переменная | Дефолт | Описание |
|---|---|---|
| `JWT_SECRET` | `dev-secret-...` | **Сменить в проде**, мин. 32 байта |
| `SECURE_COOKIES` | `true` | Флаг `Secure` на cookie (только HTTPS) |

### OTP

| Переменная | Дефолт | Описание |
|---|---|---|
| `OTP_TTL_SECONDS` | `300` | Время жизни OTP (5 минут) |
| `OTP_MAX_ATTEMPTS` | `3` | Макс. неверных попыток ввода OTP |
| `NOTIFICATION_API_URL` | `http://notification_api:8005` | Куда auth_api шлёт OTP-коды для доставки (SMS, fallback — Max-бот) |
| `INTERNAL_OTP_HMAC_KEY` | `` (пусто) | Общий с notification_api ключ для подписи `/internal/api/v1/otp/send`. Пустой → запросы идут без подписи (dev only) |
| `INTERNAL_STEP_UP_HMAC_KEY` | `` (пусто) | Общий с user_api и locker_api ключ для подписи step-up flow (`/internal/api/v1/step-up/{issue,verify}` входящие + `/internal/user/{id}/max-context` исходящие). Пустой → unsigned (dev only) |

### Токены

| Переменная | Дефолт | Описание |
|---|---|---|
| `ACCESS_TOKEN_TTL_HOURS` | `720` | Время жизни сессии (30 дней) |

### Rate limiting

| Переменная | Дефолт | Описание |
|---|---|---|
| `LOGIN_RATE_LIMIT` | `10` | Макс. попыток входа с одного IP |
| `LOGIN_RATE_WINDOW_SECONDS` | `60` | Окно rate limit (секунды) |

---

## База данных

Схема: `app/auth_data/db/migrations/001_init.sql`
Применяется автоматически при инициализации контейнера `postgres_auth`.

### Таблица `otp_codes`

```sql
CREATE TABLE otp_codes (
    id         BIGSERIAL   PRIMARY KEY,
    phone      VARCHAR(20) NOT NULL,
    code_hash  VARCHAR(64) NOT NULL,   -- SHA-256(code)
    attempts   SMALLINT    NOT NULL DEFAULT 0,
    expires_at TIMESTAMPTZ NOT NULL,
    used_at    TIMESTAMPTZ,            -- NULL = ещё не использован
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
-- Поиск активного OTP по телефону
CREATE INDEX idx_otp_phone ON otp_codes (phone, expires_at) WHERE used_at IS NULL;
```

### Таблица `access_tokens`

```sql
CREATE TABLE access_tokens (
    id          BIGSERIAL   PRIMARY KEY,
    user_id     BIGINT      NOT NULL,
    token_hash  VARCHAR(64) NOT NULL UNIQUE,  -- SHA-256(opaque_token)
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ,                  -- NULL = активен
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    user_agent  TEXT,
    ip_address  INET
);
-- Быстрый поиск по хэшу (только активные)
CREATE INDEX idx_at_hash ON access_tokens (token_hash) WHERE revoked_at IS NULL;
-- Все токены пользователя
CREATE INDEX idx_at_user ON access_tokens (user_id);
```

### Периодическая очистка

Фоновая задача в `main.py` раз в час удаляет истёкшие записи:

```python
await token_repo.delete_expired()  # DELETE FROM access_tokens WHERE expires_at < now()
await otp_repo.delete_expired()    # DELETE FROM otp_codes WHERE expires_at < now()
```

---

## Разработка

### Зависимости (устанавливаются через apt, не pip)

```
python3-tornado          # Async web framework
python3-psycopg2         # PostgreSQL
python3-dacite           # dataclass ← dict (ORM-like)
python3-bcrypt           # Хэши паролей (в тестах)
python3-jwt              # JWT (зарезервировано)
python3-requests         # HTTP-клиент (тесты)
python3-typing-extensions
python3-pytest
```

### Shared модули

Сервис использует `backend/shared/`:

| Модуль | Назначение |
|---|---|
| `shared.database.AsyncPostgres` | Async-обёртка над psycopg2 |
| `shared.database.PostgresConfig` | Конфигурация подключения к БД |
| `shared.auth.JSONHandler` | Базовый Tornado handler |
| `shared.http.RetryHTTPClient` | Inter-service HTTP с retry (использует `UserApiClient`) |
| `shared.http.ProblemType` / `ProblemTitle` | URI и title'ы RFC 7807 ошибок |
| `shared.utils.hash_password` | Client/transport-side SHA-256 пред-хэш пароля (историческое имя). Финальное хранилище — `bcrypt(sha256(plaintext))` в `scud_users.user_auth.password_hash`: user_api оборачивает SHA-256 в bcrypt (`bcrypt.hashpw`), проверка — `bcrypt.checkpw` в `internal_auth.py`. Функцию не переименовываем. |

### Inter-service HTTP

Все вызовы к `user_api` (`/internal/auth/check`, `/internal/user/by-phone`,
`/internal/user/{id}`) идут через `app.services.user_api_client.UserApiClient`,
который оборачивает `shared.http.RetryHTTPClient` (3 попытки, exp backoff).

Семантика возврата:

- HTTP 200 → распарсенный dict;
- HTTP 401 / 404 → `None` (legitimate "wrong credentials" / "user not found");
- исчерпанные retry на 5xx/network или неожиданный 4xx → `UserApiUnavailableError`,
  handler ловит и отвечает **503** с `ProblemType.SERVICE_UNAVAILABLE`.

Один экземпляр `UserApiClient` создаётся в `main.py` и кладётся в
`application.settings["user_api_client"]` — handlers берут оттуда.

### Локальный запуск

Требуется запущенный PostgreSQL `scud_auth` и User API.

```bash
# Запустить зависимости
docker compose up -d postgres_auth user_api

# Запустить auth_api локально
cd backend
export DB_HOST=localhost DB_PORT=5435 DB_USER=admin DB_PASSWORD=password DB_NAME=scud_auth
export USER_API_URL=http://localhost:8000
export SECURE_COOKIES=false
python3 -m app.main
```

### Добавление нового эндпоинта

1. Создать `app/handlers/<name>.py` с классом `class <Name>Handler(BaseHandler)`
2. Зарегистрировать маршрут в `app/main.py` → `make_app()`
3. Добавить путь в `openapi/openapi.yaml` и описание в `openapi/paths/auth.yaml`

---

## Тестирование

### Запуск через Docker Compose

```bash
docker compose --profile tests up auth_api_tests
```

Сервис `auth_api_tests` поднимает контейнер с теми же зависимостями и выполняет `pytest -vv tests`.

### Запуск локально

```bash
# Поднять БД и сервисы
docker compose up -d postgres_auth postgres_user user_api auth_api

# Переменные окружения для тестов
export TEST_BASE_URL=http://localhost:8003
export TEST_API_TIMEOUT=90
export DB_HOST=localhost
export DB_PORT=5435
export DB_USER=admin
export DB_PASSWORD=password
export DB_NAME=scud_auth
export USER_DB_HOST=localhost
export USER_DB_PORT=5432  # порт postgres_user

cd backend/services/auth_api
pytest -vv tests/integration/
```

### Как работают тесты

`conftest.py` при старте (session-scoped):
1. Ждёт доступности `postgres_auth` и `postgres_user` с ретраями
2. Применяет миграцию `001_init.sql` к auth DB
3. Создаёт тестового пользователя в User DB (телефон `+79001112244`, пароль `testpassword123`, bcrypt-хэш)

**Ключевые фикстуры:**

| Фикстура | Scope | Назначение |
|---|---|---|
| `setup_test_data` | session | Применяет миграцию, создаёт тест. пользователя |
| `wait_for_api` | session | Ждёт готовности API (polling `/login`) |
| `api_client` | function | `requests.Session` с `BASE_URL` |
| `auth_session` | function | Сессия с уже установленным `access_token` cookie |

**Вспомогательная функция:**
```python
insert_otp(phone, code, ttl_seconds=300)
# Прямая вставка OTP в БД — SMS не отправляется
```

---

## Деплой

### Docker Compose

```bash
# Полный стек
docker compose up -d postgres_auth auth_api

# Пересборка после изменений
docker compose build auth_api
docker compose up -d --force-recreate auth_api
```

**Порты:**
- `auth_api`: `8003`
- `postgres_auth`: `5435` (внешний), `5432` (внутри сети `backend_net`)

**Порядок старта:** `postgres_auth` (healthcheck) → `auth_api` → `apache_gateway`

Сервис ожидает доступности БД с ретраями (до 30 попыток, экспоненциальная задержка до 10 сек).

### Первоначальный деплой

```bash
# 1. Создать .env или выставить env vars (см. раздел Конфигурация)
# 2. Поднять БД — схема применяется автоматически через init-scripts
docker compose up -d postgres_auth

# 3. Поднять User API (нужен для проверки паролей)
docker compose up -d postgres_user user_api

# 4. Поднять Auth API
docker compose up -d auth_api

# 5. Поднять Gateway
docker compose up -d apache_gateway
```

---

## Чеклист для продакшена

- [ ] `JWT_SECRET` — сменить на 32+ случайных байта
- [ ] `SECURE_COOKIES=true` — требует HTTPS
- [ ] `INTERNAL_OTP_HMAC_KEY` — сгенерировать `openssl rand -hex 32`, выставить ОДИНАКОВЫЙ в auth_api и notification_api
- [ ] `INTERNAL_STEP_UP_HMAC_KEY` — сгенерировать `openssl rand -hex 32`, выставить ОДИНАКОВЫЙ в auth_api, user_api и locker_api
- [ ] `MAX_BOT_TOKEN` (notification_api) — токен Max-бота от @MasterBot
- [ ] `ACCESS_TOKEN_TTL_HOURS` — оценить нужное время жизни (720 = 30 дней)
- [ ] `LOGIN_RATE_LIMIT` / `LOGIN_RATE_WINDOW_SECONDS` — подобрать под ожидаемую нагрузку
- [ ] `OTP_TTL_SECONDS` — учесть задержку доставки сообщений в Max
- [ ] Включить бэкапы тома `postgres_auth_data`
- [ ] Проверить, что `/auth/validate` недоступен снаружи (Apache `Require all denied`)
- [ ] Проверить таймаут curl в `jwt_auth.lua` (5 сек) — достаточно для latency auth_api
- [ ] В `docker-compose.yaml` не держать пароли в открытом виде — использовать Docker Secrets или `.env`
