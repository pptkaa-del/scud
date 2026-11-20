FROM registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 AS production
LABEL authors="lokotkov"

# Remove the uncertified extended repo before any package install.
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt update \
    && apt dist-upgrade -y \
    && apt install -y --no-install-recommends \
                   zabbix-agent2 \
                   postgresql-client-15 \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

# Probe runner (all SQL inlined) + the UserParameter drop-in. Added here in
# D4 — the D2 image carried only the binaries.
COPY dbprobe.sh /usr/local/bin/dbprobe.sh
COPY zabbix_agent2.userparams.conf /etc/zabbix/zabbix_agent2.d/scud-dbprobe.conf
COPY zabbix_agent2.timeout.conf /etc/zabbix/zabbix_agent2.d/scud-timeout.conf
RUN chmod +x /usr/local/bin/dbprobe.sh

EXPOSE 10050
ENTRYPOINT ["/usr/sbin/zabbix_agent2", "-c", "/etc/zabbix/zabbix_agent2.conf", "-f"]
