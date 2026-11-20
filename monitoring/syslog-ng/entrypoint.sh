#!/bin/sh
# Запускает фоновый sh-таймер ежедневной ротации scud-all.log (cron в образе нет —
# одного логротейта достаточно), затем отдаёт PID 1 самому syslog-ng, чтобы
# сигналы и healthcheck работали как раньше.
set -eu

(
    while true; do
        sleep 86400
        logrotate -s /var/lib/logrotate/status /etc/logrotate.d/scud-all || true
    done
) &

exec syslog-ng -F --no-caps
