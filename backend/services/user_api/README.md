# User API

Микросервис для управления данными пользователей в системе ScudDocs.

## 📖 Документация

- **[DEVELOPER_GUIDE.md](DEVELOPER_GUIDE.md)** - Полное руководство разработчика (архитектура, структура, инструкции по добавлению функционала)
- **[tests/TESTING_GUIDE.md](tests/TESTING_GUIDE.md)** - Руководство по тестированию
- **[openapi/openapi.yaml](openapi/openapi.yaml)** - OpenAPI спецификация API

## Запуск тестов

Все тесты (unit и integration) запускаются **исключительно в Docker** —
зависимости из `requirements.txt` устанавливаются внутри контейнера, локально
их ставить не нужно. См. [tests/TESTING_GUIDE.md](tests/TESTING_GUIDE.md).

```bash
cd backend && docker compose --profile tests up --build user_api_tests
```

Команда соберёт контейнер, запустит unit + integration тесты и вернёт код
завершения тестов.

## Тестирование API (вручную)

После запуска контейнеров через `docker compose`, API доступно на порту 8000.
Все запросы требуют заголовок `X-Remote-User` с ID существующего пользователя.

### Примеры запросов

**Получение пользователя:**
```bash
curl -H "X-Remote-User: 1" http://localhost:8000/api/v1/users/1
```
Ответ (как и `GET /api/v1/users`) содержит поле `require_2fa` — включена ли
для пользователя двухфакторная аутентификация (`false`, если пароль не задан
или 2FA выключена).

**Создание пользователя:**
```bash
curl -X POST -H "X-Remote-User: 1" -H "Content-Type: application/json" \
     -d '{"phone": "+79991234567", "full_name": "Иванов Иван Иванович"}' \
     http://localhost:8000/api/v1/users
```

**Удаление (деактивация) пользователя:**
```bash
curl -X DELETE -H "X-Remote-User: 1" http://localhost:8000/api/v1/users/2
```

**Включение 2FA пользователю** (роль operator и выше):
```bash
curl -X POST -H "X-Remote-User: 1" http://localhost:8000/api/v1/user/2/enable-2fa
```
Возвращает `204`, если Max-аккаунт уже привязан; `200` с телом
(`user_id`, `require_2fa`, `max_bound`, `max_invite_token`,
`max_invite_link`, `max_invite_expires_at`) и invite-ссылкой, если ещё нет;
`409`, если у пользователя не задан пароль; `404`, если пользователь не найден.

**Выключение 2FA пользователю** (роль operator и выше):
```bash
curl -X POST -H "X-Remote-User: 1" http://localhost:8000/api/v1/user/2/disable-2fa
```
Возвращает `204`. Операция идемпотентна. `404`, если пользователь не найден.

### Защита от эскалации привилегий

Иерархия ролей: `worker` < `operator` < `administrator`. ACM пропускает
operator'а на создание/изменение пользователей и переключение 2FA, но
fine-grained проверка внутри сервиса не даёт повысить привилегии. Возвращается
`403`, если вызывающий:

- назначает (`POST /users`, `PATCH /user/{id}`) роль выше собственной;
- меняет роль / пароль / 2FA пользователя с **равной или более высокой** ролью
  (operator не может трогать другого operator'а или administrator'а);
- повышает собственную роль.

Администратор может изменять кого угодно. Роль вызывающего берётся из БД
(`users.role` по `X-Remote-User`). Матрица — `shared/access_control/user_management.py`.

## Аварийные заявки (emergency requests)

Заявка на экстренное открытие шкафа проходит два состояния: `pending`
(создана, ожидает обработки) → `closed` (закрыта оператором/администратором).

> Ролевые ограничения ниже (operator+/Допуск) применяет ACM на шлюзе
> (`http://localhost/...`). Примеры бьют напрямую в сервис (`:8000`) для ручной
> проверки — этот путь ACM минует.

**Создать заявку** (worker — только на шкаф с активным Допуском, иначе `403`;
operator/administrator — без ограничений):
```bash
curl -X POST -H "X-Remote-User: 3" -H "Content-Type: application/json" \
     -d '{"locker_id": "550e8400-e29b-41d4-a716-446655440000"}' \
     http://localhost:8000/api/v1/emergency-requests
```

**Список заявок** (фильтры `status=pending|closed`, `locker_id=<uuid>`,
`limit` 1..1000, `offset`). Worker видит только свои заявки:
```bash
curl -H "X-Remote-User: 1" \
     "http://localhost:8000/api/v1/emergency-requests?status=pending"
```

**Закрыть заявку** — это `DELETE` (отдельного `POST .../close` нет). Мягкое
закрытие: строка не удаляется, а переходит в `status='closed'` с проставлением
`closed_at`/`closed_by`. Доступно **только operator и administrator** — worker
не может закрыть заявку даже как её автор.
```bash
curl -X DELETE -H "X-Remote-User: 1" \
     http://localhost:8000/api/v1/emergency-request/42
```
Коды ответа: `204` — закрыта; `404` — заявки нет; `409` — уже закрыта
(операция не идемпотентна, повторный `DELETE` снова вернёт `409`).

**Аудит-лог заявки** (действия `created`/`closed`/`viewed`; роль operator и выше):
```bash
curl -H "X-Remote-User: 1" \
     http://localhost:8000/api/v1/emergency-request/42/logs
```

## Переменные окружения

| Переменная          | По умолчанию           | Описание                                                                          |
|---------------------|------------------------|-----------------------------------------------------------------------------------|
| `DB_HOST`           | `localhost`            | Хост БД (`scud_users`)                                                            |
| `DB_PORT`           | `5432`                 | Порт БД                                                                           |
| `DB_USER`           | `postgres`             | Пользователь БД                                                                   |
| `DB_PASSWORD`       | `postgres`             | Пароль БД                                                                         |
| `DB_NAME`           | `scud`                 | Имя БД                                                                            |
| `APP_PORT`          | `8000`                 | Порт сервиса                                                                      |
| `REDIS_URL`         | `redis://redis:6379/0` | URL Redis для cross-service инвалидации ACM-кэша. Должен включать `REDIS_PASSWORD` (см. `backend/.env.example`). |
| `ADMIN_PHONE`       | —                      | Телефон администратора, создаваемого idempotent при первом старте                 |
| `ADMIN_PASSWORD`    | —                      | Пароль администратора (требуется вместе с `ADMIN_PHONE`)                          |
| `ADMIN_FULL_NAME`   | `Администратор`        | ФИО администратора (необязательно)                                                |
| `INTERNAL_AUTH_HMAC_KEY`         | `` (пусто) | Общий с `auth_api` ключ HMAC для подписи `/internal/auth/check`                  |
| `INTERNAL_MAX_BINDING_HMAC_KEY`  | `` (пусто) | Общий с `notification_api` ключ для `/internal/user/by-invite` (привязка Max-бота)|
| `MAX_BOT_USERNAME`               | `` (пусто) | Username Max-бота; используется для сборки `max_invite_link` в ответе `POST /api/v1/users`. Пусто → `max_invite_link=null`, фронт собирает сам |

> Redis в `docker-compose.yaml` запускается с `--requirepass`, поэтому
> `REDIS_URL` должен содержать пароль (`redis://:${REDIS_PASSWORD}@redis:6379/0`).
> В compose это уже подставляется автоматически из `REDIS_PASSWORD` —
> переопределять `REDIS_URL` нужно только при деплое за пределами compose.

## Технологии
- **Python 3.11+**
- **Tornado** (Asynchronous web framework)
- **PostgreSQL** (через асинхронную обертку над `psycopg2`)
- **Pytest** (тесты — только в Docker, см. раздел «Запуск тестов»)
