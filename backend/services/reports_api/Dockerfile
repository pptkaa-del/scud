FROM registry.astralinux.ru/library/astra/ubi18-python311:1.8.5 AS production
LABEL authors="lokotkov"

RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt update \
    && apt dist-upgrade -y \
    && apt install -y --no-install-recommends \
                   python3-typing-extensions \
                   python3-dacite \
                   python3-tornado \
                   python3-psycopg2 \
                   python3-requests \
                   python3-prometheus-client \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /app

# Копируем shared компоненты (контекст сборки - backend/)
COPY shared /app/shared

# Копируем только app/ сервиса — tests/ в production не нужны (тулинг
# pytest добавляется в test-стадии вместе с самими тестами).
COPY services/reports_api/app /app/reports_api/app

WORKDIR /app/reports_api

ENTRYPOINT ["python3", "-m", "app.main"]

# === test stage — pytest + coverage tooling. =================================
# Инварианты multi-stage: production НЕ содержит pytest/extended; здесь
# поверх production ставится python3-pytest (Astra main) и coverage-tooling
# (extended, подключается ровно под два пакета и сразу удаляется), плюс
# копируются tests/. target=test обязателен в compose (см. *_tests сервисы).
FROM production AS test
RUN apt update \
    && apt install -y --no-install-recommends python3-pytest \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*
RUN echo 'deb https://download.astralinux.ru/astra/frozen/1.8_x86-64/1.8.5/extended-repository 1.8_x86-64 main contrib non-free' \
        > /etc/apt/sources.list.d/extended.list \
    && apt update \
    && apt install -y --no-install-recommends \
                   python3-coverage \
                   python3-pytest-cov \
    && rm /etc/apt/sources.list.d/extended.list \
    && apt clean \
    && rm -rf /var/lib/apt/lists/*
COPY services/reports_api/tests /app/reports_api/tests
