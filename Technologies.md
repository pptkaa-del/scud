# Перечень используемых технологий

Документ описывает стек проекта и обоснование выбора ключевых технологий.

## Серверная часть (backend)

- **Python 3.11** — единая версия для всех микросервисов под `backend/services/`.
  Версия зафиксирована тем, что входит в репозитории Astra Linux 1.8.x; см.
  ограничения по зависимостям ниже.
- **Tornado** (async web framework) — используется в качестве асинхронного
  HTTP-сервера REST API и для приёма MQTT-телеметрии/команд от устройств
  (relay-платы) через `shared.mqtt_client`. Выбран вместо Flask/FastAPI
  по двум причинам: (1) нативный async в одной кодовой базе с
  REST-обработчиками и MQTT-клиентом; (2) Tornado присутствует в репозиториях
  Astra Linux, тогда как FastAPI / Starlette — нет.
- **PostgreSQL 15** — единственное хранилище состояния. Используется паттерн
  database-per-service (см. ниже).
- **dacite** — десериализация JSON/dict в dataclass'ы во всех сервисах.
- **Docker / Docker Compose** — оркестрация локальной разработки и тестов;
  единственный поддерживаемый способ запуска и прогона тестов.

### Общие библиотеки (`backend/shared/`)

- `shared.database` — асинхронная обёртка над PostgreSQL (`AsyncPostgres`,
  `PostgresConfig`).
- `shared.auth` — `BaseHandler`: Tornado-обработчик с извлечением `X-Remote-User`
  и `X-Remote-Role` из gateway.
- `shared.http` — стандартные хелперы ответов, единый `ProblemDetails`
  (RFC 7807), `RetryHTTPClient` для inter-service вызовов.
- Логирование — stdlib `logging`: каждый модуль заводит
  `logging.getLogger(__name__)`, уровень из `LOG_LEVEL` (по умолчанию `INFO`).
  Отдельного логгер-модуля в `shared` нет.
- `shared.utils.rate_limit` — token-bucket для rate-limit per-connection.

## Шлюз (gateway)

- **Apache 2.4** — reverse proxy на порту 80, единая точка входа в систему.
- **mod_lua** — валидация JWT-токенов внутри Apache (`backend/gateway/lua/jwt_auth.lua`).
  После проверки токена шлюз внедряет `X-Remote-User` и `X-Remote-Role` в
  запросы к downstream-сервисам; сами сервисы доверяют этим заголовкам и не
  валидируют токен повторно.
- **curl** — HTTP-клиент в Lua-скрипте для вызова ACM при проверках доступа.

Ставка на Apache + mod_lua, а не на отдельный API-gateway-сервис на Python,
сделана для уменьшения числа компонентов в trust boundary и потому, что Apache
с mod_lua доступен в Astra Linux «из коробки».

## База данных (database-per-service)

- Шесть отдельных инстансов PostgreSQL 15 на хост-портах **5432–5437**
  (`scud_users`, `scud_lockers`, `scud_history`, `scud_auth`, `scud_notifications`,
  `scud_reports`).
- ACM собственной БД не имеет — читает `scud_users` в режиме read-only (7 сервисов,
  но 6 БД).
- `reports_api` хранит в `scud_reports` только очередь задач на генерацию отчётов;
  исходные данные тянет по HTTP из `history_api` / `user_api`.
- Миграции — обычный SQL в `backend/services/<svc>/app/<svc>_data/db/migrations/`.
- `history_api` использует помесячное партиционирование таблиц для телеметрии
  (предполагаемый поток — данные с 5000+ шкафов раз в 30 секунд).

## Деплой

- **Docker** — единственный поддерживаемый способ запуска backend.
- **Astra Linux 1.8.x base images** — образы тянутся из
  `registry.astralinux.ru`. Конкретные версии и теги см. в Dockerfile-ах
  сервисов под `backend/services/<svc>/Dockerfile`.
- **Ограничение на зависимости**: backend-сервисы (`backend/`) могут
  использовать только пакеты, доступные в репозиториях Astra Linux. Полный
  список — `available_packets.txt` в корне репозитория. Это ограничение —
  следствие сертификационных требований и отсутствия PyPI на целевой среде.
  На код вне `backend/` (эмулятор, system tests) это правило не
  распространяется.

## Эмулятор устройств (`emulator/`)

Отдельный проект, разрабатываемый параллельно. Не подпадает под
ограничения Astra Linux на пакеты.

- **Python 3.13+**
- **FastAPI** — REST-сервер управления флотом виртуальных шкафов.
- **Typer** — CLI (`scud`, `scud-server`).

## Клиентская часть

Клиент (frontend) находится **вне scope этого репозитория**. Backend
публикует REST API через Apache gateway на порту 80; клиентские
веб-интерфейсы реализуются отдельно и подключаются как обычный HTTP-клиент.
Устройства (relay-платы) подключаются отдельно по MQTT напрямую к брокеру
Mosquitto, минуя Apache gateway.
