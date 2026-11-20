# syntax=docker/dockerfile:1

# PgBouncer transaction pooler — один контейнер на каждый postgres-инстанс.
# Сертифицированный multi-stage образ Astra Linux: production-стадия ставит
# pgbouncer ИСКЛЮЧИТЕЛЬНО из certified `main` (extended физически вырезается из
# sources перед любым apt). Базовый образ ubi18-postgresql15 уже несёт
# psql/pg_isready — нужны для healthcheck и отладки `SHOW POOLS` через
# psql -p 6432 pgbouncer, а также envsubst для подстановки в ini-шаблон.

# ---- production stage: сертифицированный pgbouncer из Astra main ----
FROM registry.astralinux.ru/library/astra/ubi18-postgresql15:1.8.5 AS production
LABEL authors="lokotkov"

# Базовый образ стартует под пользователем postgres; для apt/mkdir нужен root.
# Контейнер ОБЯЗАН входить как root: entrypoint.sh пишет userlist.txt с
# паролями (0600) и chown'ит рантайм-каталоги, после чего сам демон pgbouncer
# роняется под непривилегированного пользователя postgres через `runuser`
# (pgbouncer отказывается работать под root). Поэтому финальный USER здесь —
# root намеренно; drop привилегий делает entrypoint, а не Dockerfile.
# hadolint ignore=DL3002
USER root

# Certification: физически удаляем extended-repo из sources ДО любого apt.
# pgbouncer присутствует в certified main (available_packets.txt:
# pgbouncer/stable 1.18.0-1.astra*), поэтому ставится без extended.
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && (sed -i '/extended/d' /etc/apt/sources.list.d/*.list 2>/dev/null || true) \
    && apt-get update \
    && apt-get install -y --no-install-recommends pgbouncer \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Каталог для сгенерированного entrypoint'ом ini + userlist (пишутся при старте
# из шаблона и секрета). pgbouncer-пакет создаёт пользователя postgres-группой;
# контейнер исполняется от root, чтобы entrypoint мог писать userlist.txt
# (содержит пароли — pgbouncer читает его до drop privileges, файл 0600).
RUN mkdir -p /etc/pgbouncer /var/log/pgbouncer /var/run/pgbouncer

COPY pgbouncer.ini.template /opt/pgbouncer/pgbouncer.ini.template
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 6432
ENTRYPOINT ["/entrypoint.sh"]
