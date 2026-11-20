# Операторский инструмент: полу-ручной end-to-end тест OTP через реальный Max.
# НЕ часть production-сборки и НЕ подлежит Astra-сертификации (dev-тулинг,
# как emulator/ и tests/system/) — pip из PyPI здесь допустим.
FROM python:3.11-slim

# requests — HTTP к gateway/Max; psycopg2-binary — единственное прямое чтение БД
# (SELECT users.max_user_id для детекта привязки); qrcode — ASCII-QR invite-ссылки.
RUN pip install --no-cache-dir \
        requests==2.32.3 \
        psycopg2-binary==2.9.10 \
        qrcode==8.0

WORKDIR /app
COPY max_otp_realtest.py /app/

# -u: небуферизованный вывод, чтобы интерактивные prompt'ы шли в TTY сразу.
ENTRYPOINT ["python", "-u", "max_otp_realtest.py"]
