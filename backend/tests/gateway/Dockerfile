FROM registry.astralinux.ru/library/astra/ubi18-python311:1.8.5

# Удаляем extended-repository из sources.list — только сертифицированный
# `main` репозиторий допустим.
# Шаблон идентичен 6 production Dockerfile'ам в backend/services/*/.
RUN sed -i '/extended/d' /etc/apt/sources.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        python3-requests \
        python3-pytest \
        python3-bcrypt \
        python3-psycopg2 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tests
COPY tests/gateway /tests

CMD ["python3", "-m", "pytest", "-vv", "."]
