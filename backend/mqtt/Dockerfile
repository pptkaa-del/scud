# syntax=docker/dockerfile:1

# ---- build stage: certified Astra C/C++ toolchain image ----
FROM registry.astralinux.ru/library/astra/ubi18-cpp122:1.8.5 AS build

# Certification: physically remove the extended repo before any apt use.
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && (sed -i '/extended/d' /etc/apt/sources.list.d/*.list 2>/dev/null || true)

WORKDIR /src
# Vendored upstream source committed verbatim (extracted, no archive). Copied as-is
# into the build; provenance + tarball SHA-256 in vendor/README.md.
COPY vendor/mosquitto-2.0.23/ /src/mosquitto-2.0.23/

# Build broker only, zero external libs (no TLS/cJSON/websockets/docs).
# 2.0.x has no separate "binary" target — plain make builds lib+client+src.
RUN make -C mosquitto-2.0.23 WITH_TLS=no WITH_CJSON=no WITH_WEBSOCKETS=no WITH_DOCS=no \
    && make -C mosquitto-2.0.23 WITH_TLS=no WITH_CJSON=no WITH_WEBSOCKETS=no WITH_DOCS=no install

# ---- runtime stage: minimal certified Astra base ----
FROM registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 AS production

RUN sed -i '/extended/d' /etc/apt/sources.list \
    && (sed -i '/extended/d' /etc/apt/sources.list.d/*.list 2>/dev/null || true)

# Copy built broker + tools + libmosquitto. Copy the whole /usr/local/lib dir
# (not a bare glob, which would fail the build if it matched zero files), then
# register /usr/local/lib with the dynamic linker.
COPY --from=build /usr/local/sbin/mosquitto /usr/local/sbin/mosquitto
COPY --from=build /usr/local/lib/ /usr/local/lib/
# NOTE: mosquitto_passwd is NOT copied. In Mosquitto 2.0.x it is built ONLY when
# WITH_TLS=yes (it links OpenSSL for SHA512/PBKDF2). With WITH_TLS=no — required
# for the certified zero-extra-lib broker — the binary does not exist AND is not
# needed: the broker compares password_file entries as plaintext (strcmp) when
# built without TLS (src/security_default.c mosquitto_unpwd_check_default). The
# password_file is therefore stored plaintext and the entrypoint no-ops the
# hashing step when mosquitto_passwd is absent. The baked passwordfile is EMPTY:
# the scud_backend credential is written at startup by entrypoint.sh from the
# `mqtt_password` Docker secret (or MQTT_PASSWORD env / dev fallback) — no
# plaintext credential is committed to the image.
RUN echo /usr/local/lib > /etc/ld.so.conf.d/local.conf && ldconfig

RUN useradd -r -s /usr/sbin/nologin mosquitto || true \
    && mkdir -p /mosquitto/config /opt/mqtt-defaults
# Config files are the IMAGE DEFAULTS baked into /opt/mqtt-defaults; at runtime a
# shared volume (compose) is mounted at /mosquitto/config and the entrypoint seeds
# it from these defaults if empty, so provisioning can append per-device entries.
COPY mosquitto.conf aclfile passwordfile /opt/mqtt-defaults/
COPY acl-reconcile.sh /acl-reconcile.sh
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh /acl-reconcile.sh \
    && chmod 0666 /opt/mqtt-defaults/passwordfile /opt/mqtt-defaults/aclfile

# Entrypoint runs as root so mosquitto can drop to the 'mosquitto' user (per
# mosquitto.conf `user mosquitto`) and the watcher can SIGHUP the broker.
EXPOSE 1883
ENTRYPOINT ["/entrypoint.sh"]

FROM production AS test
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt update \
    && apt install -y --no-install-recommends python3-pytest \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*
COPY tests /opt/mqtt-tests
WORKDIR /opt/mqtt-tests
ENTRYPOINT []
CMD ["python3", "-m", "pytest", "-vv"]
