FROM python:3.13-slim

WORKDIR /tests

# Yandex mirror — deb.debian.org часто блокируется корпоративными proxy.
RUN sed -i 's|http://deb.debian.org/debian|https://mirror.yandex.ru/debian|g; \
        s|http://security.debian.org/debian-security|https://mirror.yandex.ru/debian-security|g' \
        /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
