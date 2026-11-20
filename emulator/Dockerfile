FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml .
COPY src/ src/

# Pure-Python deps (gmqtt / typer / pytest) — no build toolchain required.
RUN pip install --no-cache-dir ".[all]"

# Headless relay-board MQTT device double. Point --host/--port at the broker,
# e.g. `docker run <image> --count 10 --host mosquitto --port 1883`.
ENTRYPOINT ["scud", "relay", "run"]
