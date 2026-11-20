FROM registry.astralinux.ru/library/astra/ubi18-apache24:1.8.5 AS production
LABEL authors="lokotkov"

# Remove the uncertified extended repo before any package install.
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt update \
    && apt dist-upgrade -y \
    && apt install -y --no-install-recommends \
                   php8.2-fpm \
                   php8.2-pgsql \
                   php8.2-gd \
                   php8.2-bcmath \
                   php8.2-mbstring \
                   php8.2-xml \
                   zabbix-frontend-php \
                   wget \
                   locales \
    && sed -i -E 's/^# *(en_US\.UTF-8|ru_RU\.UTF-8)/\1/' /etc/locale.gen \
    && locale-gen \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

# mod_php is absent from certified main — PHP runs through php-fpm via mod_proxy_fcgi.
RUN a2enmod proxy proxy_fcgi rewrite \
    && a2dissite 000-default

COPY zabbix.conf /etc/apache2/sites-available/zabbix.conf
RUN a2ensite zabbix

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8080
ENTRYPOINT ["/entrypoint.sh"]
