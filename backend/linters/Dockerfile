# syntax=docker/dockerfile:1.7
# Dockerized lint runner. Built and used by CI job `lint` and locally via the
# `linters` compose service (profile: lint). Single source of truth for linter
# versions — there is no parallel native install anywhere.
# Not subject to Astra certification (lint tooling, never enters production
# images). Sources live under /repo at runtime via the compose volume.

# Node.js runtime source for @redocly/cli (OpenAPI validator). Pulled from the
# registry and COPY'd into the linters stage below — avoids a
# NodeSource/nodejs.org download, which fails behind TLS-intercepting corporate
# proxies (same constraint that forces the Yandex Debian mirror rewrite below).
FROM node:20-slim AS node

FROM python:3.11-slim AS linters

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive \
    RUFF_CACHE_DIR=/tmp/.ruff_cache \
    PYTHONDONTWRITEBYTECODE=1

# Use Yandex Debian mirror (mirror.yandex.ru) — faster and more reliable
# than deb.debian.org from RU networks, and bypasses outbound-HTTP blocks
# on some corporate firewalls.
# hadolint ignore=DL3008,DL3009
RUN set -eux; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
        sed -i 's|http://deb.debian.org/debian|https://mirror.yandex.ru/debian|g; s|http://deb.debian.org/debian-security|https://mirror.yandex.ru/debian-security|g; s|http://security.debian.org/debian-security|https://mirror.yandex.ru/debian-security|g' /etc/apt/sources.list.d/debian.sources; \
    fi; \
    if [ -f /etc/apt/sources.list ]; then \
        sed -i 's|http://deb.debian.org/debian|https://mirror.yandex.ru/debian|g; s|http://deb.debian.org/debian-security|https://mirror.yandex.ru/debian-security|g; s|http://security.debian.org/debian-security|https://mirror.yandex.ru/debian-security|g' /etc/apt/sources.list; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        shellcheck \
        lua5.1 \
        luarocks \
        gcc \
        libc6-dev \
        liblua5.1-0-dev; \
    luarocks install luacheck; \
    curl -fsSL -o /usr/local/bin/hadolint \
        https://github.com/hadolint/hadolint/releases/download/v2.12.0/hadolint-Linux-x86_64; \
    chmod +x /usr/local/bin/hadolint; \
    rm -rf /var/lib/apt/lists/*

# Node.js 20 + @redocly/cli for OpenAPI spec well-formedness validation.
# redocly 2.x is the only validator we found that parses OpenAPI 3.2.0 AND
# resolves multifile $ref (openapi-spec-validator and redocly 1.x both reject
# `openapi: 3.2.0`). Ruleset lives in backend/redocly.yaml (well-formedness
# only — see that file). Node is COPY'd from the official `node:20-slim` image
# (registry pull) rather than downloaded from NodeSource/nodejs.org, which fail
# behind TLS-intercepting proxies; redocly itself comes from the npm registry.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
RUN set -eux; \
    ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm; \
    node --version; \
    npm --version; \
    npm install -g @redocly/cli@2.34.0; \
    npm cache clean --force

# Runtime deps (tornado/dacite/psycopg2-binary/bcrypt/cryptography/requests)
# are installed ALONGSIDE the linters: without them mypy sees `import tornado`
# as Any, `from_dict` as Any-returns, `RequestHandler` as cannot-subclass-Any,
# and lint fails on shared/. Versions are pip-friendly (psycopg2-binary, not
# psycopg2) and approximate each service's requirements.txt.
RUN pip install \
        ruff==0.7.4 \
        mypy==1.13.0 \
        bandit==1.7.10 \
        sqlfluff==3.2.5 \
        tornado==6.4.1 \
        dacite==1.8.1 \
        psycopg2-binary==2.9.10 \
        bcrypt==4.2.0 \
        cryptography==43.0.1 \
        requests==2.32.3 \
        types-requests==2.32.0.20240914

WORKDIR /repo
