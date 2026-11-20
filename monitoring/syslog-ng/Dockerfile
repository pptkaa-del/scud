FROM registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 AS production
LABEL authors="lokotkov"

# Remove the uncertified extended repo before any package install.
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt update \
    && apt dist-upgrade -y \
    && apt install -y --no-install-recommends \
                   syslog-ng \
                   logrotate \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

COPY syslog-ng.conf /etc/syslog-ng/syslog-ng.conf
COPY scud-logrotate.conf /etc/logrotate.d/scud-all
COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
