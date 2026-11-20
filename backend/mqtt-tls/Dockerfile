FROM registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 AS production
LABEL authors="lokotkov"

RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt update \
    && apt dist-upgrade -y \
    && apt install -y --no-install-recommends haproxy \
    && apt clean \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/lib/haproxy /etc/haproxy/certs

COPY haproxy.cfg /etc/haproxy/haproxy.cfg

EXPOSE 8883
ENTRYPOINT ["haproxy", "-W", "-db", "-f", "/etc/haproxy/haproxy.cfg"]

FROM production AS test
RUN apt update \
    && apt install -y --no-install-recommends python3-pytest \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*
COPY tests /opt/mqtt-tls-tests
WORKDIR /opt/mqtt-tls-tests
ENTRYPOINT []
CMD ["python3", "-m", "pytest", "-vv"]
