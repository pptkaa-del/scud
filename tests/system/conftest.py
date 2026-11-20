"""
Глобальные фикстуры для системных E2E-тестов.

Все взаимодействия осуществляются ТОЛЬКО через HTTP API.
Прямой доступ к базе данных не используется.

Переменные окружения (задаются в docker-compose.yaml):
  GATEWAY_URL          - URL Apache Gateway (http://apache_gateway)
  NOTIFICATION_API_URL - URL notification_api (http://notification_api:8005)
  ADMIN_PHONE          - телефон администратора (+79000000000)
  ADMIN_PASSWORD       - пароль администратора (changeme)
  TEST_TIMEOUT         - общий таймаут тестов в секундах (180)
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import requests

from utils.api_client import (
    GatewayClient,
    unique_phone,
)

logger = logging.getLogger(__name__)

# ── Конфигурация из env ────────────────────────────────────────────────────────

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://apache_gateway")
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "+79000000000")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
TEST_TIMEOUT = int(os.getenv("TEST_TIMEOUT", "180"))

# Каталог для артефактов теста (bind-mount из docker-compose). Сохранение
# включается переменной окружения SAVE_ARTIFACTS; по умолчанию (в CI) — выключено.
ARTIFACTS_DIR = "/artifacts"

# Маппинг сенсорных типов (code → id). Порядок задаёт SERIAL PK id 1-6
# (temperature=1 … door=6) — этот же порядок ожидают relay-e2e сценарии.
SENSOR_TYPES = [
    {"code": "temperature", "name": "Температура", "unit": "°C", "value_type": "numeric"},
    {"code": "humidity", "name": "Влажность", "unit": "%", "value_type": "numeric"},
    {"code": "shock", "name": "Датчик удара", "unit": "", "value_type": "boolean"},
    {"code": "power", "name": "Питание", "unit": "", "value_type": "numeric"},
    {"code": "intrusion", "name": "Датчик вторжения", "unit": "", "value_type": "boolean"},
    {"code": "door", "name": "Дверь", "unit": "", "value_type": "boolean"},
]


# ── Вспомогательные функции ────────────────────────────────────────────────────

def _wait_for_url(url: str, expected_codes: tuple[int, ...] = (200, 401, 403)) -> None:
    """Ожидать доступности URL (polling)."""
    start = time.time()
    while time.time() - start < TEST_TIMEOUT:
        try:
            resp = requests.get(url, timeout=5)
            if resp.status_code in expected_codes:
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(2)
    pytest.fail(f"URL {url} not reachable after {TEST_TIMEOUT}s")


def grant_locker_access(admin_session: GatewayClient, user_id: int, locker_id: str) -> None:
    """Выдать пользователю доступ к шкафу через HTTP API.

    Использует POST /api/v1/user/{id}/accesses через gateway.
    Требует роль operator или administrator (admin_session).
    """
    now = datetime.now(timezone.utc)
    resp = admin_session._session.post(
        f"{GATEWAY_URL}/api/v1/user/{user_id}/accesses",
        json={
            "start_at": (now - timedelta(hours=1)).isoformat(),
            "end_at": (now + timedelta(days=1)).isoformat(),
            "full_access": False,
            "locker_ids": [locker_id],
        },
        timeout=30,
    )
    assert resp.status_code == 201, (
        f"grant_locker_access failed: {resp.status_code} {resp.text}"
    )
    logger.info("Granted access via API: user_id=%d locker_id=%s", user_id, locker_id)


# ── Session-scoped фикстуры ────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def wait_for_services():
    """Ожидает доступности всех необходимых сервисов.

    Admin пользователь создаётся user_api автоматически на старте
    с require_2fa=False — никаких DB-операций не требуется.
    """
    logger.info("Waiting for services...")

    # Gateway (401 — нет токена, значит сервис работает)
    _wait_for_url(f"{GATEWAY_URL}/api/v1/users", expected_codes=(200, 401, 403))
    logger.info("Gateway is up")

    logger.info("All services are available")


@pytest.fixture(scope="session")
def admin_session(wait_for_services) -> GatewayClient:
    """Аутентифицированная сессия администратора (один логин на весь прогон).

    user_api создаёт admin на старте с require_2fa=False — логин работает напрямую.
    """
    client = GatewayClient.login(GATEWAY_URL, ADMIN_PHONE, ADMIN_PASSWORD)
    logger.info("Admin session created")
    yield client
    client.logout()


@pytest.fixture(scope="session", autouse=True)
def ensure_sensor_types(admin_session):
    """Гарантирует наличие типов датчиков с ID 1-6 в locker_api через gateway.

    SENSOR_TYPE_IDS в эмуляторе жёстко задаёт:
      temperature=1, humidity=2, shock=3, power=4, intrusion=5, door=6

    Создаёт типы в этом порядке (SERIAL PK автоматически выдаст нужные ID).
    При повторном запуске с сохранёнными volumes — 409 Conflict, пропускаем.
    """
    for stype in SENSOR_TYPES:
        result = admin_session.create_sensor_type(
            code=stype["code"],
            name=stype["name"],
            unit=stype["unit"],
            value_type=stype["value_type"],
        )
        logger.info(
            "Sensor type ready: code=%s id=%s",
            stype["code"], result.get("id"),
        )


# ── Вспомогательные фикстуры для тестов ──────────────────────────────────────

@pytest.fixture
def gateway(admin_session) -> GatewayClient:
    """GatewayClient от имени администратора (alias для удобства)."""
    return admin_session


@pytest.fixture
def save_artifact():
    """Сохранить артефакт теста на хост — опционально, для ручного просмотра.

    Возвращает функцию `_save(name, data) -> Path | None`. Запись происходит
    только если задана переменная окружения SAVE_ARTIFACTS и смонтирован каталог
    /artifacts (см. docker-compose.yaml). По умолчанию (в CI) — no-op.
    """
    def _save(name: str, data: bytes) -> Path | None:
        if not os.getenv("SAVE_ARTIFACTS"):
            return None
        out_dir = Path(ARTIFACTS_DIR)
        if not out_dir.is_dir():
            logger.warning(
                "SAVE_ARTIFACTS задан, но %s не смонтирован — пропускаю %s",
                ARTIFACTS_DIR, name,
            )
            return None
        out_path = out_dir / name
        out_path.write_bytes(data)
        logger.info("Saved artifact: %s (%d bytes)", out_path, len(data))
        return out_path

    return _save


@pytest.fixture
def worker_user(admin_session):
    """Создаёт worker-пользователя с паролем, удаляет после теста.

    Новые пользователи создаются с require_2fa=True по умолчанию, поэтому
    для воркера 2FA отключается через API (disable-2fa) перед логином —
    иначе логин вернёт max-binding-required без привязки Max.

    Returns:
        dict с ключами: id, phone, password, session (requests.Session)
    """
    phone = unique_phone()
    password = "workerpass_systest1"
    user = admin_session.create_user(
        phone=phone,
        full_name="System Test Worker",
        password=password,
        role="worker",
    )
    user_id = user["id"]

    # Отключаем 2FA, чтобы воркер логинился паролем без привязки Max.
    admin_session.disable_2fa(user_id)

    # Создать сессию для worker (2FA отключена — логин сразу работает)
    session = requests.Session()
    resp = session.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": phone, "password": password},
        timeout=30,
    )
    assert resp.status_code == 200, f"Worker login failed: {resp.text}"
    assert "access_token" in session.cookies, "Worker session cookie not set"

    yield {
        "id": user_id,
        "phone": phone,
        "password": password,
        "session": session,
    }

    # Cleanup
    session.post(f"{GATEWAY_URL}/api/v1/auth/logout", timeout=10)
    admin_session.delete_user(user_id)
