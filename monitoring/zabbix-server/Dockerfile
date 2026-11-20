FROM registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 AS production
LABEL authors="lokotkov"

# Remove the uncertified extended repo before any package install.
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt update \
    && apt dist-upgrade -y \
    && apt install -y --no-install-recommends \
                   zabbix-server-pgsql \
                   zabbix-sender \
                   postgresql-client-15 \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 10051
ENTRYPOINT ["/entrypoint.sh"]
