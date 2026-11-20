# Apache API Gateway для ScudDocs

Единая точка входа для всех микросервисов ScudDocs Backend.

## Назначение

Apache Gateway проксирует HTTP-запросы к шести микросервисам:
- **User API** (порт 8000) - управление пользователями, подрядчиками, доступами
- **Locker API** (порт 8001) - управление шкафами, датчиками, командами
- **History API** (порт 8002) - телеметрия, события безопасности, фотографии, аналитика
- **Auth API** (порт 8003) - выдача OTP, JWT-токенов, управление сессиями
- **ACM** (порт 8004) - проверка прав доступа (вызывается из mod_lua перед проксированием)
- **Notification API** (порт 8005) - SMS- и in-system-уведомления

## Маршрутизация

### Единая точка входа
```
http://localhost/api/v1
```

### User API (→ http://user_api:8000)

| Путь | Описание |
|------|----------|
| `/api/v1/users` | Пользователи |
| `/api/v1/users/{user_id}` | Операции с пользователем |
| `/api/v1/users/{user_id}/accesses` | Доступы пользователя |
| `/api/v1/accesses/{access_id}` | Управление доступами |
| `/api/v1/contractors` | Подрядчики |
| `/api/v1/contractor/{contractor_id}` | Операции с подрядчиком |
| `/api/v1/emergency-requests` | Экстренные заявки: список (GET), создать (POST) |
| `/api/v1/emergency-request/{id}` | Заявка: получить (GET), закрыть (DELETE, operator+) |
| `/api/v1/emergency-request/{id}/logs` | Аудит-лог заявки (GET, operator+) |

### Locker API (→ http://locker_api:8001)

| Путь | Описание |
|------|----------|
| `/api/v1/lockers` | Список шкафов |
| `/api/v1/lockers/{lockerId}` | Управление шкафом |
| `/api/v1/lockers/{lockerId}/state` | Состояние шкафа |
| `/api/v1/lockers/{lockerId}/sensors` | Датчики шкафа |
| `/api/v1/lockers/{lockerId}/sensors/{sensorId}` | Управление датчиком |
| `/api/v1/lockers/{lockerId}/sensors/{sensorId}/thresholds` | Пороговые значения |
| `/api/v1/lockers/{lockerId}/commands` | Команды шкафа |
| `/api/v1/sensor-types` | Типы датчиков |

### History API (→ http://history_api:8002)

| Путь | Описание |
|------|----------|
| `/api/v1/telemetry` | Телеметрия датчиков |
| `/api/v1/telemetry/list` | Список телеметрии |
| `/api/v1/events/security` | События безопасности |
| `/api/v1/commands/history` | История команд |
| `/api/v1/media` | Загрузка медиа и список медиа-записей |
| `/api/v1/media/{id}` | Метаданные и удаление медиа-записи |
| `/api/v1/events/security/{id}/media` | Список медиа для события безопасности |
| `/api/v1/analytics/sensors` | Аналитика датчиков |
| `/api/v1/analytics/lockers/{lockerId}/summary` | Сводка по шкафу |

## Запуск

### Production (порты сервисов закрыты)
```bash
cd backend
docker compose up -d
```

Gateway — единственная внешняя точка входа на порту **80**. Сервисы
8000-8005 и БД 5432-5436 не публикуются на хост: всё проходит через gateway,
а internal-эндпойнты (`/internal/*`) закрыты Apache (`Require all denied`).

### Dev-режим (открытые порты сервисов и БД)
```bash
cd backend
docker compose -f docker-compose.yaml -f docker-compose.dev.yaml up -d
```

Override поднимает контейнер `dev_proxy` (socat), публикующий на `127.0.0.1`:
- `8000-8005` → user/locker/history/auth/acm/notification API
- `5432-5436` → PostgreSQL для каждого сервиса

Loopback-биндинг означает, что порты доступны только с самого хоста (не с LAN).
Используйте этот режим для отладки IDE/pgAdmin/curl — **не запускайте dev-overlay
в production**.

### Запуск только gateway (если сервисы уже запущены)
```bash
docker compose up -d apache_gateway
```

### Проверка состояния
```bash
# Health check gateway
curl http://localhost/health

# Проверка User API через gateway
curl http://localhost/api/v1/users

# Проверка Locker API через gateway
curl http://localhost/api/v1/lockers

# Проверка History API через gateway
curl http://localhost/api/v1/telemetry/list
```

## Конфигурация

### Файлы
- `gateway/Dockerfile` - образ Apache 2.4 Alpine с mod_proxy
- `gateway/apache.conf` - правила проксирования и CORS (публичный vhost :80)
- `gateway/status.conf` - внутренний (порт 81) endpoint `mod_status` для мониторинга
- `gateway/mpm.conf` - закрепляет prefork-потолок `ServerLimit == MaxRequestWorkers` (150)

### Настройки Apache
- **ProxyTimeout**: 300 секунд (для длительных операций)
- **CORS**: разрешены все origins, методы GET/POST/PUT/DELETE/PATCH/OPTIONS
- **Логи**: stdout/stderr для интеграции с Docker

### Переменные окружения (docker-compose)
Не требуются - маршрутизация настроена через имена сервисов Docker.

## CORS

Gateway автоматически добавляет CORS headers ко всем ответам:
```
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, POST, PUT, DELETE, PATCH, OPTIONS
Access-Control-Allow-Headers: Content-Type, Authorization, X-Remote-User, X-Remote-Role, X-Remote-Lockers
Access-Control-Max-Age: 3600
```

Preflight OPTIONS requests обрабатываются на уровне gateway с кодом 200.

## Аутентификация

Gateway выполняет JWT-аутентификацию через `jwt_auth.lua` (mod_lua) и инжектирует
заголовки `X-Remote-User`, `X-Remote-Role` для downstream-сервисов. Авторизация
(per-route role checks) делегирована ACM-сервису через HTTP-вызов из Lua
(`curl http://acm:8004/acm/check`).

Поток обработки запроса:
1. Apache принимает HTTP-запрос на порт 80.
2. `jwt_auth.lua` извлекает Bearer-токен из заголовка `Authorization` и валидирует его.
3. Lua-скрипт вызывает ACM (`/acm/check`) с методом, путём и ролью пользователя.
4. При успехе скрипт инжектирует `X-Remote-User`, `X-Remote-Role`,
   `X-Remote-Lockers` и пропускает запрос в `ProxyPass`.
5. При неудаче возвращается 401 (нет/неверный токен) или 403 (запрет ACM).

Прямой доступ к downstream-сервисам в обход gateway не допускается — internal-эндпойнты
(`/internal/*`) закрыты на уровне Apache (`Require all denied`).

## Проксируемые заголовки

При успешной авторизации ACM добавляет следующие заголовки к каждому проксируемому запросу:

| Заголовок | Тип | Описание |
|---|---|---|
| `X-Remote-User` | `integer` | Идентификатор аутентифицированного пользователя |
| `X-Remote-Role` | `string` | Роль пользователя: `worker`, `operator`, `administrator` |
| `X-Remote-Lockers` | `string` | Список UUID шкафов с активным Допуском, разделённых запятой; либо `*` если у пользователя доступ ко всем шкафам |

Для ролей `operator` и `administrator` значение `X-Remote-Lockers` всегда равно `*`.

`X-Remote-Lockers` используется downstream не только как гейт доступа к **одному** шкафу
(`/api/v1/locker/{id}`), но и как **область видимости строк** в списочных эндпойнтах.
locker_api разбирает заголовок через `shared.access_control.resolve_locker_scope` и
фильтрует по нему выдачу `GET /api/v1/lockers` и `POST /api/v1/lockers/batch`: `worker`
получает только шкафы с активным Допуском, `*` снимает ограничение. Пустой/отсутствующий
заголовок для списка — не ошибка, а пустая выдача (`200`, `total: 0`); fail-closed, всего
парка при этом не видно.

Клиентские значения `X-Remote-Lockers` вычищаются на входе — заголовок выставляет
исключительно ACM.

Downstream-сервисы должны доверять этим заголовкам и **не обращаться** к User API повторно
для проверки прав. Прямой доступ к сервисам в обход gateway не допускается.

Пример заголовков, которые получает backend-сервис:
```
X-Remote-User: 42
X-Remote-Role: worker
X-Remote-Lockers: 550e8400-e29b-41d4-a716-446655440000,6ba7b810-9dad-11d1-80b4-00c04fd430c8
```

## Health Check

Gateway предоставляет эндпоинт `/health` для мониторинга:

```bash
curl http://localhost/health
```

Docker healthcheck проверяет этот эндпоинт каждые 30 секунд.

## Мониторинг (mod_status)

Детальная статистика Apache (воркеры, соединения, throughput) отдаётся через
`mod_status`, но **только внутри `backend_net`** — публичный vhost `:80` её не
раскрывает (server-status светит worker-state, URL запросов в обработке и IP
клиентов). `gateway/status.conf` поднимает отдельный `VirtualHost` на **порту
81**, который не публикуется наружу в `docker-compose.yaml` (как и `/metrics`
бэкендов), с default-deny и единственным разрешённым `SetHandler server-status`.

```bash
# Машинно-читаемый снимок (из контейнера в backend_net):
docker compose exec apache_gateway curl -s "http://localhost:81/server-status?auto"
```

`zabbix_server` (тоже в `backend_net`) скрейпит этот endpoint HTTP-агент-итемом и
строит дашборд **SCUD Gateway** — см. `monitoring/zabbix/README.md`. Ключевой
сигнал: MPM шлюза — **prefork** (один процесс на соединение), поэтому
`BusyWorkers` у предела `MaxRequestWorkers` (закреплён = 150 в `gateway/mpm.conf`,
там же `ServerLimit` = ему же, чтобы длина scoreboard = реальному потолку) =
потолок одновременных REST-соединений через шлюз. Поднять
потолок — синхронно увеличить оба значения в
`mpm.conf` (решение про память: ~неск. МБ на процесс).

`mod_status` НЕ видит HTTP-коды, поэтому edge-ошибки шлюза (502/503/504) на этом
дашборде невидимы — их считают per-service `scud_http_requests_5xx`.

## Логи

Логи Apache выводятся в stdout/stderr:

```bash
# Просмотр логов gateway
docker compose logs -f apache_gateway

# Логи всех сервисов
docker compose logs -f
```

## Масштабирование

Для горизонтального масштабирования:

1. Запустить несколько реплик каждого сервиса:
```bash
docker compose up -d --scale user_api=3 --scale locker_api=3
```

2. Добавить балансировщик перед gateway (nginx/haproxy)

3. Или использовать Docker Swarm/Kubernetes для автоматической балансировки

## Troubleshooting

### Gateway не запускается
```bash
# Проверить логи
docker compose logs apache_gateway

# Проверить синтаксис конфигурации
docker compose exec apache_gateway httpd -t
```

### 503 Service Unavailable
Один из backend-сервисов недоступен:
```bash
# Проверить статус всех сервисов
docker compose ps

# Перезапустить конкретный сервис
docker compose restart user_api
```

### 404 Not Found
Путь не найден в маршрутизации gateway. Проверьте URL и сравните с таблицей маршрутизации выше.

## Разработка

### Изменение конфигурации
1. Отредактировать `gateway/apache.conf`
2. Пересобрать образ:
```bash
docker compose build apache_gateway
docker compose up -d apache_gateway
```

### Добавление нового сервиса
1. Добавить ProxyPass правила в `gateway/apache.conf`:
```apache
ProxyPass /api/v1/new-service http://new_service:8003/api/v1/new-service
ProxyPassReverse /api/v1/new-service http://new_service:8003/api/v1/new-service
```

2. Обновить `depends_on` в docker-compose.yaml

3. Пересобрать и перезапустить

## Архитектура

```
┌─────────────┐
│   Client    │
└──────┬──────┘
       │ :80
       │
┌──────▼─────────────────────────────────────┐
│   Apache Gateway (port 80)                 │
│   - CORS                                    │
│   - mod_lua jwt_auth.lua (JWT validation)  │
│   - Proxy routing                           │
│   - Health checks                           │
└──┬─────────────────────────────────────────┘
   │ curl http://acm:8004/acm/check
   │ (per-request authorization)
   │ on success → ProxyPass:
   │
   ├─ :8000 ─→ user_api          (пользователи, доступы, подрядчики)
   ├─ :8001 ─→ locker_api        (шкафы, датчики, команды)
   ├─ :8002 ─→ history_api       (телеметрия, события, фотографии)
   ├─ :8003 ─→ auth_api          (OTP, JWT, сессии)
   ├─ :8004 ─→ acm               (контроль доступа — также вызывается из mod_lua)
   └─ :8005 ─→ notification_api  (SMS / in-system уведомления)
```

## Production Checklist

- [ ] Настроить SSL/TLS (добавить mod_ssl)
- [ ] Проверить политику ротации JWT-секрета (`jwt_auth.lua` + auth_api)
- [ ] Настроить rate limiting (mod_ratelimit)
- [ ] Добавить логирование в централизованную систему (ELK/Loki)
- [ ] Настроить мониторинг метрик (Prometheus exporter)
- [ ] Включить HTTP/2
- [ ] Настроить кэширование (mod_cache)
- [ ] Ограничить CORS origins (заменить * на конкретные домены)
- [ ] Настроить security headers (HSTS, CSP, X-Frame-Options)

## Ссылки

- [Apache mod_proxy документация](https://httpd.apache.org/docs/2.4/mod/mod_proxy.html)
- [OpenAPI спецификации](../services/*/openapi/openapi.yaml)
- [Основной docker-compose.yaml](../docker-compose.yaml)
