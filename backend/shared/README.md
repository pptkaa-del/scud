# `shared` — общие библиотеки backend-сервисов

Пакет `shared` содержит код, переиспользуемый всеми сервисами под
`backend/services/`. Это **единственный источник истины** для перечисленных ниже
абстракций — не переопределяйте их в отдельных сервисах.

Пакет монтируется в каждый сервисный образ и импортируется как `shared.*`
(например `from shared.auth import ProblemDetailsHandler`). Тесты пакета лежат в
`shared/tests/{unit,integration}/` и запускаются контейнером `shared_tests`
(`docker compose --profile tests up --build shared_tests`).

## Логирование

Структурированного модуля `shared.logging` **нет**. Логирование настраивается
через stdlib: каждый модуль заводит `logger = logging.getLogger(__name__)`,
уровень управляется переменной окружения `LOG_LEVEL` (по умолчанию `INFO`).
Все HTTP-хендлеры, WS-события и межсервисные вызовы пишут через
module-level логгеры. Никогда не логируем пароли, OTP-коды, содержимое JWT и
сессионные токены.

## Каталог модулей

| Модуль | Импорт | Назначение |
|---|---|---|
| `shared.database` | `AsyncPostgres`, `PostgresConfig`, `DatabaseError`, `QueryError`, `UnexpectedEmptyResultError`, `SqlParam`, `is_foreign_key_violation`, `is_unique_violation`, `require_found` | Async-обёртка над PostgreSQL. Размер пула — `DB_MAX_WORKERS` / `DB_MIN_CONNECTIONS` (per-service override в compose). |
| `shared.auth` | `ProblemDetailsHandler`, `JSONHandler`, `UserAuthMixin` | Базовые Tornado-хендлеры (RFC 7807 JSON) и auth-mixin. Импортировать **только из `shared.auth`**, не из приватного `base_handler`. |
| `shared.http` | `RetryHTTPClient`, `ProblemDetails`, `ProblemType`, `ProblemTitle`, `STATUS_TO_PROBLEM`, `UpstreamUnavailableError` | `RetryHTTPClient` — singleton для **всех** межсервисных вызовов (не инстанцируйте `AsyncHTTPClient` напрямую). `ProblemDetails` — RFC 7807. |
| `shared.utils` | `read_secret`, `is_production`, `hash_password`, `hash_token`, `body_sha256`, `signed_path`, `compute_request_signature`, `verify_request_signature` | `read_secret(name, env_var=, default=, required=)` (Docker secret → env → default). HMAC-SHA256 подпись межсервисных запросов привязана к `timestamp\nMETHOD\npath\nsha256(body)`; `signed_path` складывает канонизированный query в подписываемый путь. |
| `shared.utils.rate_limit` | `TokenBucket` | In-memory token bucket для per-connection rate-limit (в рамках одного процесса). |
| `shared.access_control.commands` | `check_command_permission`, `COMMAND_PERMISSION_MATRIX`, `OTP_REQUIRED_COMMANDS` | Матрица «роль × тип команды» (default-deny). Источник истины для fine-grained авторизации команд лока. Worker → OPEN/CLOSE; operator/administrator → +REBOOT (CONFIG выведен из эксплуатации). |
| `shared.access_control` | `can_assign_role`, `can_modify_target`, `is_self_promotion`, `ROLE_RANK`, `require_locker_grant`, `canonical_locker_uuid`, `canonicalize_grant_set` | Проверки управления пользователями (privilege-escalation guard) и канонизация UUID лока для grant-проверок. |
| `shared.metrics` | `http_requests_total`, `http_request_duration_seconds`, `db_query_duration_seconds`, `db_pool_connections_in_use`, `db_pool_connections_idle`, `db_pool_connections_waiters`, `db_pool_connections_max`, `register_pool`, `update_pool_gauges`, `command_2fa_challenges_total`, `command_2fa_delivery_total`, `generate_latest`, `CONTENT_TYPE_LATEST`, `SERVICE_NAME` | Prometheus-инструментирование. Маршруты авто-нормализуются (`/locker/:id`) для ограничения cardinality. DB-pool-gauge'и семплируются pull-based на каждый scrape `/metrics` (`update_pool_gauges()` в `MetricsHandler`), не на hot-path acquire/release; label `pool` = имя БД. Каждый сервис вызывает `register_pool(db.pool_name, db.get_pool_info)` после создания пула. |
| `shared.redis_client` | `RedisClient`, `RedisError`, `RedisConnectionError` | Минимальный async RESP-клиент (GET/SET/DEL/PING) поверх `asyncio`+`socket`. `python3-redis` нет в сертифицированном `main` — не ставьте его через pip. |
| `shared.mqtt_client` | `MqttClient`, `MqttQoS` | Тонкая async-обёртка над вендоренным `gmqtt` (`shared/_vendor/gmqtt`, без pip в сертифицированном образе). connect/subscribe/publish/handler, resubscribe при reconnect. |
| `shared.xlsx` | `write_styled_table`, `write_table`, `Database`, `writexl` | Генерация `.xlsx` чистым stdlib (zipfile + OOXML), без сторонних пакетов — совместимо с сертификацией Astra. `write_styled_table` (объединённый заголовок, цветная шапка, рамки, «зебра», авто-ширина, freeze, автофильтр) экранирует formula-инъекцию. |
| `shared.search` | `normalize_query`, `like_filter`, `escape_like`, `build_like_pattern` | Унифицированный подстрочный ILIKE-поиск для списочных ручек (query-параметр `q`). LIKE-метасимволы экранируются. **Имена колонок в `like_filter` обязаны быть из whitelist** (подставляются в SQL как есть). |
| `shared.dtos` | `PaginatedResponse[T]` | Generic-обёртка пагинации. Не переопределяйте per-service. |
| `shared.enums` | `UserState`, `UserRole`, `SecurityEventType`, `EventSeverity`, … | Кросс-сервисные enum'ы. Новые общие enum'ы добавляйте сюда, а не в per-service модули. |
| `shared.test_fixtures` | `wait_for_db`, `wait_for_api`, `APIClient` | Хелперы интеграционных тестов. Используйте в сервисных `conftest.py` вместо дублирования polling/retry-логики. |

## Правила использования

- **Не инстанцируйте** `tornado.httpclient.AsyncHTTPClient` напрямую — все
  межсервисные вызовы идут через singleton `RetryHTTPClient`.
- Для любого нового секрета используйте `read_secret(...)`, а не прямое чтение
  файла/`os.getenv`.
- При добавлении межсервисного эндпоинта, затрагивающего учётные
  данные/чувствительное состояние, переиспользуйте
  `compute_request_signature`/`verify_request_signature` (+ `signed_path`, если
  селектор лежит в query).
- Перед добавлением зависимости проверяйте сертификацию Astra: пакет должен быть
  в `available_packets.txt` (репозиторий `main`). Если его нет — пишите
  минимальную stdlib-обёртку (примеры: `redis_client.py`, `mqtt_client.py`,
  `xlsx/styled.py`).
