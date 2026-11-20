# Max OTP — полу-ручной end-to-end тест

Прогоняет всю цепочку второго фактора через **живой** Max-бот и реальный
телефон с приложением Max. Скрипт автоматизирует API-шаги и паузится на двух
действиях, которые может сделать только человек: нажать «Начать» в боте и
прочитать пришедший код.

Это **операторский инструмент, а не автотест** — он требует живого аккаунта и
ручного ввода, поэтому в CI не запускается (отдельный профиль `manual`).

## Что проверяется

```
0. GET  {MAX_BOT_API_URL}/me          — токен бота валиден
1. POST /api/v1/auth/login (админ)     — сессия (у админа require_2fa=false)
2. POST /api/v1/users                  — создание юзера + invite-ссылка
3. POST /api/v1/user/{id}/enable-2fa   — ВКЛючение 2FA через API + invite-ссылка
4. [человек] открыть ссылку в Max, нажать «Начать»
5. SELECT users.max_user_id            — poller поймал bot_started и привязал
6. POST /api/v1/auth/login             — реальная отправка OTP в Max
7. [человек] прочитать код в Max
8. POST /api/v1/auth/verify            — код принят, сессия выдана
```

## Предусловия

1. **Боевой токен бота** в `backend/secrets/max_bot_token` (из `@MasterBot`).
   ```bash
   printf '%s' '<ТОКЕН_ИЗ_MASTERBOT>' > backend/secrets/max_bot_token
   ```
2. **`MAX_BOT_USERNAME`** — username бота, чтобы собрать invite-ссылку
   (`https://max.ru/<username>?start=<token>`). Можно задать в `.env` или
   передать переменной окружения. Если `GET /me` его вернёт — подхватится сам.
3. **Стек поднят с этим токеном.** `notification_api` стартует поллер только
   при непустом `MAX_BOT_TOKEN`:
   ```bash
   cd backend && docker compose up -d --build
   ```
4. Телефон с установленным Max, на котором нажмёшь «Начать» и прочитаешь код.

## Запуск

Из каталога `backend/` (интерактивно — нужен TTY, поэтому `run`, не `up`):

```bash
docker compose --profile manual run --rm --build max_otp_realtest
```

Скрипт проведёт по шагам и сам подскажет, когда открыть ссылку и ввести код.

## Переменные окружения

| Переменная | Дефолт | Назначение |
|---|---|---|
| `GATEWAY_URL` | `http://apache_gateway` | точка входа API |
| `ADMIN_PHONE` / `ADMIN_PASSWORD` | `+79000000000` / `changeme` | админ для создания юзера |
| `MAX_BOT_API_URL` | `https://platform-api.max.ru` | REST Max-бота |
| `MAX_BOT_USERNAME` | — | username бота для invite-ссылки |
| `DB_HOST`/`DB_PORT`/`DB_USER`/`DB_NAME` | `postgres_user`/`5432`/`admin`/`scud_users` | БД для чтения max_user_id (детект bot_started) |
| `BIND_TIMEOUT_S` | `300` | сколько ждать нажатия «Начать» |
| `TEST_PASSWORD` / `TEST_FULL_NAME` | см. скрипт | данные тестового юзера |

Токен бота и пароль БД читаются из Docker secrets (`max_bot_token`,
`postgres_user_password`) с fallback на env.

## Включение 2FA через API (шаг 3) и единственное чтение БД (шаг 5)

Раньше пробел был в том, что публичного API для включения 2FA не существовало, и
инструмент дёргал флаг прямым `UPDATE user_auth.require_2fa = TRUE`. Теперь этот
пробел **закрыт**: включение 2FA идёт через `POST /api/v1/user/{id}/enable-2fa`
(OPERATOR+). Для непривязанного пользователя эндпоинт возвращает `200` со свежей
invite-ссылкой (`max_invite_link`) — её инструмент и открывает на шаге 4; для уже
привязанного — `204`.

Единственное прямое обращение к БД, которое осталось, — это **чтение**
`users.max_user_id` на шаге 5. Так инструмент без спама по API детектит, что
`bot_started` долетел до поллера и привязка состоялась. Записей в БД инструмент
больше не делает.
