# ACM — Модуль контроля разделения доступа

Tornado-микросервис (порт **8004**), реализующий ролевую авторизацию (Access Control Matrix).

ACM принимает запрос с заголовками `X-Access-Token`, `X-Request-Method`,
`X-Request-Path` от gateway, валидирует токен через `auth_api`, затем проверяет,
разрешён ли данный (метод, путь) для роли пользователя. Правила определены
в `app/rules.py`, валидация имеет TTL-кэш (`app/cache.py`).

Вызывается Apache Lua (`jwt_auth.lua`) перед каждым проксируемым запросом.
Проверяет токен сессии, роль пользователя и наличие Допуска, возвращает
контекстные заголовки для downstream-сервисов.

## Назначение

```
Client
  ↓ Cookie: access_token=...
Apache gateway (mod_lua: jwt_auth.lua)
  ↓ GET /acm/check  (X-Access-Token, X-Request-Method, X-Request-Path)
ACM  (порт 8004)
  ↓ GET /auth/validate              → auth_api:8003
  ↓ SELECT role, state FROM users   → postgres_user
  ↓ SELECT active accesses          → postgres_user
  ← 200: X-User-Id, X-Remote-Role, X-Remote-Lockers
  ← 401 / 403
Apache: выставляет заголовки → проксирует запрос
Backend-сервис: читает X-Remote-User, X-Remote-Role, X-Remote-Lockers
```

## Эндпоинты

| Путь            | Метод | Описание                                         |
|-----------------|-------|---------------------------------------------------|
| `/acm/check`    | GET   | Проверка прав (вызывается Lua, не клиентами)     |
| `/acm/identity` | GET   | Резолвинг session-cookie в роль, **без** проверки прав по пути. Используется gateway'ем (`static_route.lua`) для выбора ролевой папки фронтенда. 200 + `X-Remote-Role` / 401 (нет, невалиден, истёк токен, либо пользователь деактивирован) / 503 (auth_api недоступен, fail closed). |
| `/health`       | GET   | Health check (статус сервиса и БД)               |

Полная спецификация: [`openapi/openapi.yaml`](openapi/openapi.yaml)

## Конфигурация

Переменные окружения:

| Переменная         | По умолчанию             | Описание                                  |
|--------------------|--------------------------|-------------------------------------------|
| `DB_HOST`          | `postgres_user`          | Хост БД user_api (scud_users)             |
| `DB_PORT`          | `5432`                   | Порт БД                                   |
| `DB_USER`          | `acm_ro`                 | Least-privilege роль БД (SELECT-only, SEC-24) |
| `DB_PASSWORD`      | `acm_ro_dev_password`    | Пароль роли `acm_ro` (секрет `acm_db_password`) |
| `DB_NAME`          | `scud_users`             | Имя БД                                    |
| `AUTH_API_URL`     | `http://auth_api:8003`   | URL auth_api для валидации токенов        |
| `APP_PORT`         | `8004`                   | Порт сервиса                              |
| `CACHE_TTL_SECONDS`| `30`                     | TTL кэша роли + Допусков пользователя (с) |
| `REDIS_URL`        | `redis://redis:6379/0`   | URL Redis для backing-store кэша и cross-service инвалидации. Должен включать `REDIS_PASSWORD` (см. `backend/.env.example`). |

> Redis запускается с `--requirepass`, поэтому `REDIS_URL` должен содержать
> пароль (`redis://:${REDIS_PASSWORD}@redis:6379/0`). В compose это уже
> подставляется автоматически из `REDIS_PASSWORD`.

## Архитектура

```
app/
├── main.py       — Tornado app, точка входа
├── rules.py      — Матрица разрешений: Role enum, Rule dataclass, check_access()
├── cache.py      — In-memory TTL-кэш UserContext (роль + список шкафов)
├── db.py         — UserRepository: read-only запросы к scud_users
└── handlers/
    ├── base.py    — BaseHandler с RFC 7807 ProblemDetails
    ├── check.py   — GET /acm/check — основная логика
    ├── identity.py — GET /acm/identity — резолвинг сессии в роль, без проверки прав по пути
    └── health.py  — GET /health
```

### Матрица разрешений (`rules.py`)

Роли и иерархия:
```
administrator (3) → operator (2) → worker (1)
```

Правила — упорядоченный список `Rule(method, path_regex, min_role)`.
Срабатывает первое совпадение. Все несовпавшие пути — **запрещены**.

Для `worker` + locker-scoped пути (содержащего UUID шкафа) дополнительно
проверяется наличие активного Допуска (`X-Remote-Lockers`).

**Коллекция шкафов — только по роли; фильтрация строк делегирована locker_api.**
В пути `GET /api/v1/lockers` (и `POST /api/v1/lockers/batch`) UUID нет, поэтому
`check_access` решает лишь «пускать ли роль» (`worker+` для списка, `operator+`
для батча) и физически не может отобрать нужные строки: ACM читает `scud_users`,
а шкафы живут в `scud_lockers` — чужой БД, к которой у роли `acm_ro` нет доступа.
Тот же Допуск ACM отдаёт в `X-Remote-Lockers` (список UUID либо `*` для
`operator`/`administrator`/`full_access`), а locker_api превращает его в
SQL-фильтр по строкам (`shared.access_control.resolve_locker_scope`). Итог:
`worker` получает `200` со списком только своих шкафов, а не весь парк.

**Разные методы одного ресурса — разные роли.** Аварийные заявки показывают это
наглядно:
- `POST /api/v1/emergency-requests` — `worker+` (worker'у дополнительно нужен
  активный Допуск на шкаф; тонкая проверка — в user_api);
- `GET /api/v1/emergency-request/{id}` — `worker+`, но worker видит только свои;
- `DELETE /api/v1/emergency-request/{id}` (закрыть заявку) — `operator+`;
- `GET /api/v1/emergency-request/{id}/logs` — `operator+`.

### Кэш (`cache.py`)

`UserContextCache` — потокобезопасный словарь `user_id → UserContext` с TTL.
По умолчанию TTL = 30 секунд. Деактивированные пользователи не кэшируются.

### БД (`db.py`)

Подключается к `scud_users` (та же БД, что у `user_api`) в режиме read-only.
Использует `AsyncPostgres` из `shared/database` + `dacite.from_dict` для маппинга строк.

## Запуск

```bash
# Все сервисы включая ACM
cd backend
docker compose up -d

# Только ACM (если остальные сервисы уже запущены)
docker compose up -d acm
```

## Тесты

### Unit-тесты (без зависимостей)

```bash
# Из контейнера или локально с установленными зависимостями
pytest tests/unit -v
```

Покрывают:
- `test_rules.py` — матрица разрешений: Role enum, Rule.matches, find_rule, is_locker_scoped, check_access
- `test_cache.py` — TTL-кэш: set/get, истечение, инвалидация, evict_expired

### Интеграционные тесты

```bash
# Запускают все зависимые сервисы и тесты ACM
docker compose --profile tests run --rm acm_tests
```

Покрывают:
- Публичные пути (без токена)
- Валидацию токена (401)
- Права по роли: worker / operator / administrator
- Допуск на конкретный шкаф для worker
- Деактивированный пользователь (403)
- Ответные заголовки (X-User-Id, X-Remote-Role, X-Remote-Lockers)

## Безопасность

- Эндпоинт `/acm/check` **недоступен снаружи** — заблокирован в `apache.conf` (`Require all denied` для `/acm`)
- ACM не хранит токены — валидация делегируется `auth_api`
- Кэш хранит только `user_id → role + locker_ids` (не токены)
- Все запросы к БД — read-only транзакции

## Production Checklist

- [ ] Использовать отдельного read-only пользователя БД для ACM
- [ ] Настроить `CACHE_TTL_SECONDS` в соответствии с требованиями к актуальности прав
- [ ] Убедиться что `/acm` закрыт снаружи в `apache.conf`
- [ ] Мониторить `/health` в системе наблюдаемости
