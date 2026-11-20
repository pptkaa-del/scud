# Notification API

Подсистема уведомлений (Подсистема 6 по Architecture.md) системы ScudDocs СКУД.

## Типы уведомлений

| Тип | Описание | Получатели |
|-----|----------|------------|
| `persistent` | Постоянное уведомление внутри Системы | Роли и/или ID пользователей |

## API Endpoints

| Метод | Путь | Описание | Мин. роль |
|-------|------|----------|-----------|
| `POST` | `/api/v1/notifications` | Создать уведомление | operator |
| `GET` | `/api/v1/notifications` | Список с фильтрацией | worker |
| `GET` | `/api/v1/notification/{id}` | Получить по ID | worker |
| `DELETE` | `/api/v1/notification/{id}` | Отменить уведомление | operator |

### Фильтрация (GET /api/v1/notifications)

| Параметр | Тип | Описание |
|----------|-----|----------|
| `type` | string | Фильтр по типу (`persistent`) |
| `user_id` | int | Уведомления, нацеленные на пользователя |
| `role` | string | Уведомления, нацеленные на роль |
| `tag_key` + `tag_value` | string | Фильтрация по тегам |
| `active_only` | bool | Только активные (default: `true`) |
| `limit` | int | Лимит (default: `100`) |
| `offset` | int | Смещение (default: `0`) |

## Теги

Уведомлениям можно задавать произвольные теги формата `ключ: значение` через поле `tags`.
Теги используются для фильтрации и позволяют другим подсистемам находить свои уведомления
без жёсткой связности.

Пример:
```json
{
  "tags": {
    "locker_id": "550e8400-e29b-41d4-a716-446655440000",
    "severity": "high",
    "source": "state_monitor"
  }
}
```

## Фоновый планировщик

Сервис содержит фоновый процесс `NotificationScheduler`, который чистит
Max-outbox: удаляет просроченные/терминальные OTP-записи, чтобы plaintext
второго фактора не оседал в БД (SEC-18).

Интервал проверки настраивается через `SCHEDULER_INTERVAL_SECONDS` (default: 30).

## Доставка OTP (Подсистема 5 / 2FA): SMS-primary, MAX-fallback

Сервис доставляет одноразовые коды второго фактора по двум каналам:
**SMS** (шлюз Yeastar TG200, через `wg_gateway` sidecar — см. ниже) —
основной канал, и мессенджер Max (`https://platform-api.max.ru`) —
fallback/канал для пользователей без телефона в запросе.

- `POST /internal/api/v1/otp/send` — закрытый эндпоинт, вызывается
  `auth_api` при `/api/v1/auth/login`. Тело:
  `{user_id, code, max_user_id, address, ttl_seconds}`. `channel` в
  теле нет — канал выбирает сам notification_api (SMS, если передан
  `address` и SMS-транспорт настроен; иначе MAX). Подпись
  `X-Internal-Timestamp` + `X-Internal-Signature` (HMAC-SHA256 поверх
  `timestamp + METHOD + path + sha256(body)`, ключ `INTERNAL_OTP_HMAC_KEY`).
  Ответы: 204 (доставлено по SMS или по MAX, включая fallback) / 200
  `{"status": "queued_until_binding"}` (ни один транспорт не доступен —
  dev/CI без настроенного SMS/MAX, либо MAX-пользователь ещё не привязан;
  это НЕ штатный путь для обычного SMS-адресата) / 401 / 503 (SMS и
  MAX-fallback оба недоступны).
- `MaxBotPoller` — фоновый long-polling воркер. Делает `GET /updates`
  Max API, ловит `bot_started` с `start_payload=<invite_token>`,
  идёт в `user_api` `POST /internal/user/by-invite` (HMAC ключ
  `INTERNAL_MAX_BINDING_HMAC_KEY`) — там атомарно проставляется
  `users.max_user_id`. Затем доставляет накопившиеся `outbound_max_messages`.

При пустом `SMS_GATEWAY_URL`/кредах шлюза SMS-транспорт не поднимается —
OTP уходит по MAX (или в очередь, если MAX тоже не настроен/пользователь
не привязан). При пустом `MAX_BOT_TOKEN` MAX-транспорт и `MaxBotPoller`
не запускаются. Если не настроен ни один транспорт, сервис стартует в
degraded-режиме: `/internal/api/v1/otp/send` возвращает
`queued_until_binding`, OTP-коды копятся в outbox. Использовать **только
для локальной разработки**.

### SMS-шлюз (Yeastar TG200) — sidecar `wg_gateway`, профиль `sms`

Шлюз доступен только через WireGuard-туннель. Sidecar `wg_gateway`
(`monitoring/wg-gateway/`, профиль compose `sms`) поднимает туннель и
форвардит порт шлюза внутрь `backend_net`; `notification_api` обращается
к нему как к обычному хосту (`SMS_GATEWAY_URL=http://wg_gateway:7777`) и
никогда не видит сам туннель. Профиль `sms` не стартует при обычном
`docker compose up` — подробности подъёма и секреты WG/SMS см.
`backend/DEPLOYMENT.md` → «SMS-шлюз (Yeastar TG200 через WireGuard)».

## Конфигурация

| Переменная | Default | Описание |
|-----------|---------|----------|
| `DB_HOST` | `localhost` | Хост PostgreSQL |
| `DB_PORT` | `5432` | Порт PostgreSQL |
| `DB_USER` | `postgres` | Пользователь БД |
| `DB_PASSWORD` | `postgres` | Пароль БД |
| `DB_NAME` | `scud_notifications` | Имя БД |
| `APP_PORT` | `8005` | Порт API |
| `SCHEDULER_INTERVAL_SECONDS` | `30` | Интервал планировщика (сек) |
| `MAX_BOT_API_URL` | `https://platform-api.max.ru` | URL REST API Max-бота |
| `MAX_BOT_TOKEN` | `` (пусто) | Токен бота от `@MasterBot`; пусто → Max не подключён, поллер отключён |
| `INTERNAL_OTP_HMAC_KEY` | `` (пусто) | Общий с `auth_api` ключ HMAC; пусто → подпись не проверяется (dev only) |
| `INTERNAL_MAX_BINDING_HMAC_KEY` | `` (пусто) | Общий с `user_api` ключ HMAC; пусто → запросы поллера идут без подписи (dev only) |
| `USER_API_URL` | `http://user_api:8000` | Куда поллер шлёт `/internal/user/by-invite` |
| `SMS_GATEWAY_URL` | `` (пусто) | URL SMS-шлюза Yeastar TG200 (через `wg_gateway` sidecar, профиль `sms`); пусто → SMS-транспорт не поднимается, OTP уходит по MAX/в очередь |
| `SMS_MIN_INTERVAL_SECONDS` | `10` | Минимальный интервал между отправками на один GSM-порт шлюза (аппаратный лимит ~1 SMS/10 c); FIFO-пейсер в `SmsGatewayClient` |
| `SMS_MAX_QUEUE_WAIT_SECONDS` | `10` | Максимальное ожидание очереди пейсера; дольше — отказ без обращения к шлюзу (`UpstreamUnavailableError`, далее fallback на MAX/503) |
| `SMS_GATEWAY_TIMEOUT_SECONDS` | `25` | Таймаут запроса к шлюзу (отклик шлюза ~3–15 с); без ретраев |
| `MAX_SEND_TIMEOUT_SECONDS` | `8` | Таймаут отправки сообщения в Max (доставка OTP); long-polling `/updates` остаётся на 35 с |
| `sms_api_user` *(secret)* | `` (пусто) | Логин HTTP-CGI API Yeastar TG200; `/run/secrets/sms_api_user` → env `SMS_API_USER`; пусто → SMS-транспорт не поднимается |
| `sms_api_password` *(secret)* | `` (пусто) | Пароль HTTP-CGI API Yeastar TG200; `/run/secrets/sms_api_password` → env `SMS_API_PASSWORD`; пусто → SMS-транспорт не поднимается |

## Запуск

```bash
# Весь стек
docker compose up notification_api

# Только тесты
docker compose --profile tests up notification_api_tests --attach notification_api_tests
```

## OpenAPI

Спецификация: `openapi/openapi.yaml`

```bash
# ReDoc
docker compose -f docker-compose.docs.yaml up
# http://localhost:8084
```
