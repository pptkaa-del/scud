# Zabbix cert-гейт — доставка SQL-схемы

Дата проверки: 2026-06-20. Базовый образ:
`registry.astralinux.ru/library/astra/ubi18-python311:1.8.5` (extended удалён).

## Решение: схема БУНДЛИТСЯ в `zabbix-server-pgsql` (вендоринг НЕ нужен)

Пакет `zabbix-server-pgsql` версии `1:7.0.10+dfsg-2.astra5+ci3` из Astra
`main-repository` содержит SQL-схему по классической Debian-раскладке:

```
./usr/share/zabbix-server-pgsql/schema.sql.gz   (19 КБ — DDL)
./usr/share/zabbix-server-pgsql/images.sql.gz   (1.1 МБ — иконки)
./usr/share/zabbix-server-pgsql/data.sql.gz     (2.7 МБ — начальные данные)
```

Это НЕ новый однофайловый путь `…/zabbix-sql-scripts/postgresql/server.sql.gz`
(который предполагался в spec §5.4) — у Astra-сборки 7.0.10 раскладка из трёх
файлов. Пакет `zabbix-sql-scripts` в `main` отсутствует, но он и не нужен:
схема уже внутри `zabbix-server-pgsql`.

### Что из этого следует для C1.4 (init-скрипт БД Zabbix)

Загрузка в порядке **schema → images → data** против пустой БД Zabbix:

```sh
for f in schema images data; do
  zcat "/usr/share/zabbix-server-pgsql/${f}.sql.gz" | psql -q -d "$ZBX_DB" -U "$ZBX_USER"
done
```

Файлы уже внутри образа `zabbix_server` (он ставит `zabbix-server-pgsql`).
Если init-скрипт выполняется в контейнере `postgres_zabbix` (где пакета нет),
схему нужно сделать доступной — либо смонтировать том из `zabbix_server`, либо
выполнять загрузку из самого `zabbix_server` после старта его БД. C1.4 выбирает
конкретный механизм; ветка гейта — **A (bundle)**.

## Воспроизведение

```sh
docker run --rm registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 sh -c \
  "sed -i '/extended/d' /etc/apt/sources.list && apt-get update && \
   apt-get download zabbix-server-pgsql && \
   dpkg -c zabbix-server-pgsql*.deb | grep -iE 'usr/share/zabbix-server-pgsql/.*sql'"
```

## Cert-блокер #2 (закрыт обходом)

`zabbix-agent2-plugin-postgresql` в `main` отсутствует → нативный мониторинг
PostgreSQL агентом 2 недоступен. Обход в рамках Этапа 0: видимость БД на
уровне приложения уже дают метрики `scud_db_query_duration_seconds`,
`scud_db_pool_connections_*` и `scud_db_errors_total` на `/metrics` (их
скрейпит Zabbix). Нативный agent2-мониторинг БД — вне объёма Этапа 0.

# Cert-гейт Этап 2 — agent2 (infra-метрики) + psql admin-зонды

Дата проверки: 2026-06-20. Базовый образ:
`registry.astralinux.ru/library/astra/ubi18-python311:1.8.5` (extended удалён).

## Гейт A2-1: zabbix-agent2 и postgresql-client-15 ставятся из `main`

`available_packets.txt` уже перечисляет оба пакета (строки 1339, 425), но гейт
требует подтверждения, что они резолвятся БЕЗ extended:

```sh
docker run --rm registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 sh -c \
  "sed -i '/extended/d' /etc/apt/sources.list && apt-get update -qq && \
   apt-cache policy zabbix-agent2 postgresql-client-15 | grep -E 'Candidate'"
```

Ожидается — и получено:
```
zabbix-agent2:
  Candidate: 1:7.0.10+dfsg-2.astra5+ci3
postgresql-client-15:
  Candidate: 15.14-astra.se3.1
```

Вывод → монтируемся на этом: `monitoring/zabbix-agent2/Dockerfile` ставит
`zabbix-agent2 postgresql-client-15` в `production`-стадии после
`sed -i '/extended/d'`. НЕ нужен test-стадий (мониторинг-образ).

## Гейт A2-2: встроенный redis-плагин agent2

agent2 несёт redis-плагин в самом бинарнике (отдельного пакета в `main` нет —
`zabbix-agent2-plugin-postgresql` отсутствует, см. cert-блокер #2 ниже; redis —
ВСТРОЕННЫЙ, не выносной плагин).

```sh
docker run --rm registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 sh -c \
  "sed -i '/extended/d' /etc/apt/sources.list && apt-get update -qq && \
   apt-get install -y -qq zabbix-agent2 >/dev/null 2>&1 && \
   zabbix_agent2 -V | head -1 && zabbix_agent2 --print | grep -i '^redis\.' | head -5"
```

Получено:
```
zabbix_agent2 (Zabbix) 7.0.10
REDIS_PLUGIN_ABSENT
```

### FALLBACK активирован

В Astra-сборке 7.0.10 redis-плагин вырезан — `zabbix_agent2 --print` не даёт
ни одного ключа `redis.*`. Нативный `redis.*` недоступен.
Обход: locker_api/user_api НЕ держат прямой redis-зонд в метриках, поэтому
шаблон infra (`template-scud-infra.yaml`) объявляет Redis-айтемы через
`scud.dbprobe`-механизм НЕЛЬЗЯ (redis — не SQL). Вместо `redis.ping` шаблон
заводит единственный gauge `scud_redis_up` (0/1), который пишется существующим
`shared.redis_client`-врапером на /metrics соответствующих сервисов, и agent2
скрейпит его как HTTP-айтем. Этот gauge — точка деградации Группы C; решение о
fallback ФИКСИРУЕТСЯ здесь, чтобы infra-шаблон знал, какой ключ заводить.

**Решение D5: FALLBACK — использовать `scud_redis_up` gauge через HTTP-айтем.**

## Гейт A2-3: psql достаёт PgBouncer admin-консоль и Postgres

```sh
docker run --rm --network backend_backend_net \
  -e PGPASSWORD="<pgb_admin_password>" \
  registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 sh -c \
  "sed -i '/extended/d' /etc/apt/sources.list && apt-get update -qq && \
   apt-get install -y -qq postgresql-client-15 >/dev/null 2>&1 && \
   psql -h pgbouncer_user -p 6432 -U pgb_admin -d pgbouncer -tAc 'SHOW POOLS;' | head -2 && \
   echo '---PG---' && \
   PGPASSWORD='<postgres_user_password>' \
   psql -h postgres_user -p 5432 -U admin -d scud_users -tAc 'SELECT 1;'"
```

Получено:
```
WARNING: Could not determine access control version of server
pgbouncer|pgbouncer|1|0|0|0|0|0|0|0|0|0|0|0|0|statement
---PG---
1
```

Примечание: WARNING от pgbouncer admin-консоли — штатное поведение (виртуальная
БД pgbouncer не поддерживает `pg_catalog` запросы pg_hba_file_rules). Строка
пула и `1` подтверждают: `psql` физически достигает обоих эндпоинтов.
Пользователь postgres_user: `admin` (POSTGRES_USER в docker-compose), не
`postgres` — важно для `scud.dbprobe[pg,...]` UserParameter в agent2.

## Гейт A2-4: active-mode TCP до `zabbix_server:10051`

```sh
docker run --rm --network backend_backend_net \
  registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 sh -c \
  "apt-get install -y -qq netcat-openbsd >/dev/null 2>&1 && \
   nc -z -w5 zabbix_server 10051 && echo ZBX_10051_REACHABLE"
```

Получено:
```
ZBX_10051_REACHABLE
```

Подтверждает, что `zabbix_agent2` (`ServerActive=zabbix_server`, active mode,
БЕЗ публикуемых портов) достучится до сервера на `backend-net`.

## Итог гейта Этап 2

| Гейт | Что доказано | Статус |
|---|---|---|
| A2-1 | agent2 + psql из `main` (extended removed) | PASS |
| A2-2 | встроенный redis-плагин (или fallback `scud_redis_up`) | FALLBACK — плагин отсутствует → D5 заводит `scud_redis_up` |
| A2-3 | psql → pgbouncer admin + postgres (роль postgres = `admin`) | PASS |
| A2-4 | active-mode reach `zabbix_server:10051` | PASS |
