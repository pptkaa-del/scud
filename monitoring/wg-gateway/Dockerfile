# Сетевой инфра-sidecar (VPN + TCP-forward). Вне периметра Astra-сертификации,
# как host-TLS-терминатор: у WireGuard нет сертифицированного аналога, а
# компонент не обрабатывает прикладные данные СКУД. Не production backend-образ.
FROM ubuntu:24.04
RUN apt-get update \
    && apt-get install -y --no-install-recommends wireguard-tools socat iproute2 iptables ca-certificates \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]
