# PgBouncer — транзакционный пулинг соединений

Один контейнер PgBouncer на каждый из 6 PostgreSQL-инстансов (`user`, `locker`,
`history`, `auth`, `notification`, `reports`). Каждый production-сервис ходит в
свою БД не напрямую, а через соответствующий пулер `pgbouncer_<instance>:6432`.

## Зачем

При горизонтальном масштабировании сервисов суммарное число коннектов к
PostgreSQL растёт линейно: N реплик × размер пула каждой = быстро упирается в
`max_connections` (дефолт 100) и приводит к отказам `FATAL: too many
connections`. PgBouncer в режиме `pool_mode=transaction` мультиплексирует много
коротких клиентских транзакций на небольшой фикс-пул серверных соединений,
поэтому потолок коннектов к postgres перестаёт зависеть от числа реплик —
он равен сумме `default_pool_size` всех пулеров.

## Почему transaction-режим здесь безопасен

`pool_mode=transaction` возвращает серверное соединение в пул на ГРАНИЦЕ каждой
транзакции, поэтому состояние, переживающее границу транзакции на одном
соединении, ломается. Наш код этого состояния не создаёт:

- каждый вызов `AsyncPostgres` (`execute` / `fetch_one` / `fetch_all`) — это одна
  короткая single-statement транзакция (BEGIN → один `execute` → COMMIT);
- НЕТ prepared statements на уровне сессии, advisory locks, `LISTEN`/`NOTIFY`,
  session-level `SET` (только `SET LOCAL` внутри транзакции — безопасно);
- outbox-диспетчер забирает строки одним `UPDATE ... RETURNING` поверх
  `FOR UPDATE SKIP LOCKED` — тоже single-statement.

Инвариант зафиксирован в `backend/CONVENTIONS.md` и `backend/DEPLOYMENT.md`:
любая новая БД-операция обязана оставаться single-statement, иначе пулинг
сломается тихо. См. также `ignore_startup_parameters` ниже.

## `ignore_startup_parameters`

psycopg2 при подключении шлёт startup-параметр `application_name` (например ACM
передаёт `acm`, а `AsyncPostgres` по умолчанию `async-postgres-connector`) и
иногда `extra_float_digits`. В transaction-режиме PgBouncer не может прокинуть
их на каждое серверное соединение, поэтому без
`ignore_startup_parameters = extra_float_digits,application_name` подключение
падает с `unsupported startup parameter`. Параметр обязателен — не удалять.

## Аутентификация (userlist)

`auth_type = scram-sha-256`. `entrypoint.sh` на старте генерирует
`/etc/pgbouncer/userlist.txt` из смонтированных Docker-секретов (Variant B):
пароли пишутся как plaintext-литералы (`"role" "password"`), а PgBouncer
считает SCRAM-верификацию на лету и для клиента, и при логине в postgres. Файл
создаётся с правами `0600`; пароли НИКОГДА не логируются.

- `pgbouncer_user` монтирует ДВА секрета и пишет ДВЕ роли: `admin`
  (`postgres_user_password`) для user_api и `acm_ro` (`acm_db_password`) для ACM
  (SEC-24 least-privilege — ACM ходит в `scud_users` под read-only ролью).
- остальные 5 пулеров монтируют только свой `postgres_<svc>_password` и пишут
  роль `admin`.
- в каждый userlist добавляется локальная роль `pgb_admin` (admin/stats) для
  диагностики через `psql -p 6432 pgbouncer`. Пароль этой роли задаётся Docker
  secret `pgb_admin_password` (см. ниже).

### `pgb_admin_password` — Docker secret для stats-роли

`pgb_admin` — pgbouncer-локальная stats-роль (`admin_users`/`stats_users`);
она **не существует в PostgreSQL** и не видит прикладных данных — только
`SHOW POOLS` / `SHOW STATS` виртуальной БД `pgbouncer`.

`pgb_admin_password` — Docker secret, который монтируется:

- во все 6 контейнеров PgBouncer: `entrypoint.sh` вписывает его в `userlist.txt`
  при каждом старте (вместе с app-паролями `postgres_<svc>_password`);
- в контейнер `zabbix_agent2` (профиль `monitoring`): `dbprobe.sh` использует его
  для подключения к admin-консоли (`psql -p 6432 -U pgb_admin pgbouncer -c 'SHOW POOLS;'`).

Сидируется в dev через `backend/scripts/seed-dev-secrets.sh`. В production:

```bash
openssl rand -hex 16 > backend/secrets/pgb_admin_password
chmod 600 backend/secrets/pgb_admin_password
```

Ротация: обновить файл секрета, затем пересоздать все 6 пулеров и `zabbix_agent2`
(«ALTER USER» не нужен — это не роль PostgreSQL). Подробная процедура в
`backend/DEPLOYMENT.md → "Ротация pgb_admin_password"`.

### Ротация паролей

PgBouncer читает userlist при старте. После ротации пароля роли в postgres
(`ALTER USER ... PASSWORD ...` или замена секрета `postgres_<svc>_password` /
`acm_db_password`) перезапустите соответствующий пулер
(`docker compose up -d --force-recreate pgbouncer_<instance>`), чтобы он
перегенерировал userlist из нового секрета. Порядок ротации описан в
`backend/DEPLOYMENT.md → "Ротация секретов"`.

## Тюнинг пула

Размеры — env-driven (дефолты в `docker-compose.yaml`):

| env | назначение |
|---|---|
| `PGB_DEFAULT_POOL_SIZE` | серверных соединений к postgres на (роль, БД) |
| `PGB_RESERVE_POOL_SIZE` | доп. соединения при всплеске (выдаются с задержкой) |
| `PGB_MAX_CLIENT_CONN` | потолок клиентских подключений к самому пулеру |

Бюджет: сумма `default_pool_size + reserve_pool_size` по всем 6 пулерам должна
укладываться в postgres `max_connections` (дефолт 100) с запасом на superuser и
служебные подключения. Низко-QPS-инстансы (`acm`/user, auth, notification,
reports) держат пул небольшим (10–15), горячие (`locker`, `history`) — 25–30.

## Диагностика

```bash
# Пулы и статистика (роль pgb_admin, БД 'pgbouncer' — виртуальная)
docker compose exec pgbouncer_user psql -h 127.0.0.1 -p 6432 -U pgb_admin pgbouncer -c "SHOW POOLS;"
docker compose exec pgbouncer_user psql -h 127.0.0.1 -p 6432 -U pgb_admin pgbouncer -c "SHOW STATS;"
docker compose logs -f pgbouncer_user
```

`SHOW POOLS` показывает `sv_active` / `sv_idle` (серверные соединения к postgres)
и `cl_active` / `cl_waiting` (клиентские). При насыщении `cl_waiting` растёт, а
`sv_active` не превышает `default_pool_size` — это и есть ограничение коннектов.
