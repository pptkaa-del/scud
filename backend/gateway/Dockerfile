# Apache Gateway для ScudDocs Backend
# Проксирует запросы к микросервисам User API, Locker API, History API

FROM registry.astralinux.ru/library/astra/ubi18-apache24:1.8.5

# Включаем необходимые модули Apache (Debian-style).
# `mod_status` включён, но публичный vhost :80 его НЕ отдаёт — статистика
# доступна ТОЛЬКО на внутреннем порту 81 (gateway/status.conf), не
# публикуемом наружу. Дистрибутивный mods-enabled/status.conf (глобальный
# <Location /server-status> + ExtendedStatus) удаляется, чтобы server-status
# не появился на :80; свой ExtendedStatus задаём в status.conf.
# /health по-прежнему отдаётся через mod_lua (см. gateway/lua/health.lua).
RUN a2enmod proxy && \
    a2enmod proxy_http && \
    a2enmod rewrite && \
    a2enmod headers && \
    a2enmod lua && \
    a2enmod remoteip && \
    a2enmod status && \
    rm -f /etc/apache2/mods-enabled/status.conf

# Устанавливаем глобальный ServerName для подавления warning
RUN echo "ServerName localhost" >> /etc/apache2/apache2.conf

# Копируем конфигурацию шлюза + Lua-обработчики
COPY gateway/apache.conf /etc/apache2/sites-available/gateway.conf
COPY gateway/lua/ /etc/apache2/lua/

# Внутренний (порт 81) endpoint server-status для мониторинга. conf-enabled
# подключается глобально из apache2.conf, поэтому достаточно положить файл
# туда — отдельный a2enconf не нужен. Не публикуется наружу.
COPY gateway/status.conf /etc/apache2/conf-enabled/scud-status.conf

# Закрепляем prefork-потолок: ServerLimit == MaxRequestWorkers, чтобы длина
# scoreboard (= знаменатель worker_util_pct в мониторинге) совпадала с реальным
# потолком одновременных соединений. conf-enabled грузится ПОСЛЕ
# mods-enabled/mpm_prefork.conf, поэтому эти значения побеждают.
COPY gateway/mpm.conf /etc/apache2/conf-enabled/scud-mpm.conf

# Entrypoint подставляет env-переменные (CORS_ALLOWED_ORIGIN) в конфиг
# до запуска Apache.
COPY gateway/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Отключаем дефолтный сайт и включаем наш
RUN a2dissite 000-default && \
    a2ensite gateway

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
