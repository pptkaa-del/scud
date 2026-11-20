"""
Сценарий: Step-up 2FA для привилегированных команд шкафа.

Покрывает полный happy-path: worker с require_2fa=true и привязанным
max_user_id → POST /commands{type=open} получает 202 + challenge_id →
извлекаем OTP-код из notification_db (outbox) → POST /commands/confirm с
кодом → 201 + command body → команда доходит до релейного двойника
(status=executed).

Также проверяет, что close с теми же установками идёт мимо step-up flow:
сразу 201 без challenge (close не входит в OTP_REQUIRED_COMMANDS).

Зависимости:
- gateway → acm (X-Remote-Require-2FA) → locker_api (issue → 202)
- auth_api (step_up_challenges) → notification_api (outbox, purpose='otp_command')
- locker_api confirm → auth_api verify → command enqueue → RelayRunner → history_api

Чтение OTP-кода из outbox делается через docker SDK exec в контейнер
``backend-postgres_notification-1`` (docker.sock пробрасывается в
docker-compose.yaml read-only). Это тот же приём, что
``utils/service_logs.py`` использует для чтения docker-логов.

Регрессия (test_reports.py) защищена тем, что фикстура ``worker_user`` в
conftest.py вызывает ``disable_2fa(user_id)`` — мы используем отдельный
flow с прямой манипуляцией БД, не трогая существующую фикстуру.
"""

from __future__ import annotations

import logging
import re
import time

from conftest import GATEWAY_URL, grant_locker_access
import docker
import pytest
import requests
from utils.api_client import GatewayClient, unique_phone
from utils.mqtt_locker import _create_mqtt_locker, _random_relay_id
from utils.relay_runner import RelayRunner
from utils.wait import wait_for

logger = logging.getLogger(__name__)

# Имя контейнера postgres_notification — фиксируется docker-compose проектом
# (backend). При смене prefix-а проекта обновить здесь и в utils/service_logs.py.
_NOTIFICATION_DB_CONTAINER = "backend-postgres_notification-1"
_USER_DB_CONTAINER = "backend-postgres_user-1"
_AUTH_DB_CONTAINER = "backend-postgres_auth-1"

# Запас под тик command-queue процессора (.env.fast: 2с) + доставку rep.
_COMMAND_TIMEOUT_S = 60.0
_ONLINE_TIMEOUT_S = 45.0

# База для виртуального max_user_id (фактический = база + user_id для UNIQUE).
# Реального Max-аккаунта за этим ID нет — notification_api получит 404 от
# Max API, но outbox-запись будет создана ДО попытки доставки, поэтому
# тест прочитает OTP-код из БД, минуя Max.
_FAKE_MAX_USER_ID = 9_000_000_001


def _docker_exec(container: str, *cmd: str) -> str:
    """Выполнить команду в контейнере и вернуть stdout как str.

    Использует ту же docker SDK, что и utils/service_logs.py — docker.sock
    пробрасывается в system_tests read-only.
    """
    client = docker.from_env()
    container_obj = client.containers.get(container)
    exec_result = container_obj.exec_run(cmd, demux=False)
    if exec_result.exit_code != 0:
        raise RuntimeError(
            f"docker exec failed in {container}: exit={exec_result.exit_code} "
            f"output={exec_result.output!r}"
        )
    output = exec_result.output
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def _set_user_max_id(user_id: int, max_user_id: int) -> None:
    """Привязать max_user_id напрямую в БД (тестовая оптимизация).

    Нет публичного admin-эндпоинта для установки max_user_id — он выдаётся
    user_api только через invite/bot_started flow. Прямая запись в БД того
    же класса, что и чтение OTP из outbox: тестовая обвязка без production
    риска (system_tests не публикуется).
    """
    _docker_exec(
        _USER_DB_CONTAINER,
        "psql", "-U", "admin", "-d", "scud_users", "-tA", "-c",
        f"UPDATE users SET max_user_id = {max_user_id} WHERE id = {user_id};",
    )


def _enable_2fa_in_db(user_id: int) -> None:
    """Включить require_2fa для пользователя напрямую в БД.

    Обходим админский POST /enable-2fa, потому что после него надо ещё
    инвалидировать ACM-кэш для свежего ctx — а доступа к ACM cache flush
    из system_tests нет. После прямой записи в БД ACM подгружает свежий
    контекст при первом /acm/check (singleflight TTL 30s; для нового
    пользователя первая проверка всегда из БД).
    """
    _docker_exec(
        _USER_DB_CONTAINER,
        "psql", "-U", "admin", "-d", "scud_users", "-tA", "-c",
        f"UPDATE user_auth SET require_2fa = TRUE WHERE user_id = {user_id};",
    )


def _peek_outbox_code(user_id: int, purpose: str) -> str | None:
    """Извлечь 6-значный OTP-код из последней outbox-записи пользователя.

    Фильтруется по purpose ('otp_login' | 'otp_command'), чтобы тест не
    зацепил stale login-код предыдущей сессии (defence in depth поверх
    явной очистки outbox перед командой).
    """
    sql = (
        "SELECT body FROM outbound_max_messages "
        f"WHERE user_id = {user_id} AND purpose = '{purpose}' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    output = _docker_exec(
        _NOTIFICATION_DB_CONTAINER,
        "psql", "-U", "admin", "-d", "scud_notifications", "-tA", "-c", sql,
    )
    match = re.search(r"\b(\d{6})\b", output)
    return match.group(1) if match else None


def _wait_for_otp(user_id: int, purpose: str, timeout: float = 30.0) -> str:
    """Поллить outbox, пока появится OTP-код с указанным purpose."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        code = _peek_outbox_code(user_id, purpose)
        if code:
            return code
        time.sleep(1)
    raise TimeoutError(
        f"OTP code with purpose={purpose!r} not found in outbox for "
        f"user_id={user_id} within {timeout}s"
    )


def _clear_outbox(user_id: int) -> None:
    """Удалить все outbox-записи пользователя (между login/command чтобы
    polling не зацепил stale-код).

    Без очистки login-код мог бы быть прочитан как command-код, если бы
    purpose-фильтр был неточный; явная очистка — defence in depth.
    """
    _docker_exec(
        _NOTIFICATION_DB_CONTAINER,
        "psql", "-U", "admin", "-d", "scud_notifications", "-tA", "-c",
        f"DELETE FROM outbound_max_messages WHERE user_id = {user_id};",
    )


def _peek_latest_challenge_id(user_id: int, command_type: str) -> str | None:
    """Достать challenge_id из step_up_challenges, минуя HTTP-ответ.

    Используется как fallback, когда `POST /commands` вернул 503 из-за
    недоступности Max-API (dev-стек без живого max_user_id за fake-токеном).
    Запись в step_up_challenges создаётся ДО попытки доставки OTP, поэтому
    она в БД даже при 503-ответе клиенту.
    """
    sql = (
        "SELECT id::text FROM step_up_challenges "
        f"WHERE user_id = {user_id} AND used_at IS NULL "
        f"AND scope_kind = 'locker_command' "
        f"AND scope_payload->>'command_type' = '{command_type}' "
        "ORDER BY created_at DESC LIMIT 1"
    )
    output = _docker_exec(
        _AUTH_DB_CONTAINER,
        "psql", "-U", "admin", "-d", "scud_auth", "-tA", "-c", sql,
    )
    cleaned = output.strip()
    return cleaned if cleaned else None


@pytest.fixture
def worker_2fa(admin_session: GatewayClient):
    """Worker с require_2fa=True (выставляется ПОСЛЕ логина) + max_user_id.

    Тонкость: dev-стек настроен с реальным MAX_BOT_TOKEN, но fake_max_user_id
    отдаст 404 от Max API → notification_api помечает outbox failed и отвечает
    503. Поэтому login через 2FA-flow здесь не сработает (он шлёт OTP через
    Max). Чтобы пройти аутентификацию, логинимся с require_2fa=FALSE (cookie
    выдаётся напрямую), затем выставляем require_2fa=TRUE в БД — ACM-кэш для
    нового пользователя ещё пуст (login — публичный путь, не дёргает ACM),
    поэтому первый /api/v1/locker/.../commands попадёт в ACM с require_2fa=TRUE
    и пойдёт через step-up.

    Это тот же паттерн, что используется в backend/tests/gateway/
    test_require_2fa_header.py.

    Для step-up issue notification_api пишет outbox-запись с кодом ДО
    попытки доставки через Max, поэтому даже когда Max возвращает 404,
    challenge_id уже выдан и код лежит в БД — тест его прочитает напрямую.
    """
    phone = unique_phone()
    password = "workerpass_2fa_systest"
    user = admin_session.create_user(
        phone=phone,
        full_name="System Test Worker 2FA",
        password=password,
        role="worker",
    )
    user_id = user["id"]

    # 1. Сначала снимаем 2FA, чтобы залогиниться напрямую паролем (без Max).
    admin_session.disable_2fa(user_id)

    session = requests.Session()
    login_resp = session.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": phone, "password": password},
        timeout=30,
    )
    assert login_resp.status_code == 200, (
        f"worker_2fa setup: pre-login (non-2FA) failed: {login_resp.status_code} "
        f"{login_resp.text}"
    )
    assert "access_token" in session.cookies, (
        "worker_2fa setup: access_token cookie missing after pre-login"
    )

    # 2. Уникальный max_user_id (UNIQUE WHERE state != 'deleted'). Реального
    # Max бота за ним нет — это ожидаемо: для step-up issue важно только
    # наличие max_user_id != NULL, чтобы получить настоящий challenge_id.
    fake_max_id = _FAKE_MAX_USER_ID + user_id
    _set_user_max_id(user_id, fake_max_id)

    # 3. Теперь поднимаем require_2fa=TRUE. ACM ещё не запросили для этого
    # пользователя через защищённый путь (login публичный), поэтому
    # ctx-кэш для user_id пуст; первый /acm/check возьмёт свежий контекст
    # из БД.
    _enable_2fa_in_db(user_id)

    yield {
        "id": user_id,
        "phone": phone,
        "password": password,
        "max_user_id": fake_max_id,
        "session": session,
    }

    # Cleanup
    try:
        _clear_outbox(user_id)
    except Exception as exc:
        logger.warning("Failed to clear outbox for user %s: %s", user_id, exc)
    try:
        session.post(f"{GATEWAY_URL}/api/v1/auth/logout", timeout=10)
    except Exception as exc:
        logger.warning("Worker session logout failed: %s", exc)
    admin_session.delete_user(user_id)


class TestCommandStepUp2FA:
    """Step-up 2FA flow для команд шкафа."""

    def test_open_with_2fa_requires_step_up(
        self,
        gateway: GatewayClient,
        worker_2fa: dict,
    ):
        """
        Полный happy-path:
        1. worker с require_2fa=true + max_user_id (см. фикстуру worker_2fa).
        2. Поднимаем релейный шкаф, выдаём worker'у доступ.
        3. POST /commands{open} → 202 + challenge_id (step-up issue).
        4. Читаем OTP-код из outbox (purpose='otp_command').
        5. POST /commands/confirm{challenge_id, code} → 201 + command body.
        6. Релейный двойник выполняет команду → history фиксирует status=executed.
        """
        worker_id = worker_2fa["id"]
        worker_session = worker_2fa["session"]

        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            with RelayRunner(relay_id) as runner:
                runner.publish_stat()
                grant_locker_access(gateway, worker_id, locker_id)

                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                    message=f"Locker {locker_id} did not come online",
                )
                logger.info("Locker is online: %s", locker_id)

                # Очищаем outbox перед командой, чтобы polling нашёл именно
                # command-OTP. Должен быть пустым после фикстуры, но это явная
                # защита от stale-кода предыдущих тестов.
                _clear_outbox(worker_id)

                # Step-up: POST /commands → 202 + challenge_id (issue).
                # Если в dev-стеке установлен max_bot_token (для tools/max-otp-realtest),
                # notification_api будет реально звонить в Max API и получит 404 на
                # fake_max_user_id → отдаст 503. Это пробрасывается в locker_api → 503.
                # Так как notification_api пишет outbox-запись с OTP-кодом ДО попытки
                # доставки, А auth_api пишет step_up_challenges ДО вызова notification,
                # мы можем достать и challenge_id, и код из БД напрямую при таком 503.
                # Если max_bot_token пуст (seed-dev-secrets.sh default) — 202 с
                # challenge_id в теле, читаем как обычно.
                issue = worker_session.post(
                    f"{GATEWAY_URL}/api/v1/locker/{locker_id}/commands",
                    json={"type": "open", "parameters": {}, "max_retries": 3},
                    timeout=30,
                )
                assert issue.status_code in (202, 503), (
                    f"expected 202 step-up challenge (or 503 on Max-misconfig), "
                    f"got {issue.status_code} {issue.text}"
                )
                if issue.status_code == 202:
                    issue_body = issue.json()
                    challenge_id = issue_body.get("challenge_id")
                    assert challenge_id, f"challenge_id missing: {issue_body}"
                    assert "expires_at" in issue_body
                    logger.info(
                        "Got step-up challenge from 202 body: id=%s delivery=%s",
                        challenge_id, issue_body.get("delivery_status"),
                    )
                else:
                    # max_bot_token задан и Max API вернул 404 на fake_max_user_id.
                    # Challenge всё равно записан в scud_auth.step_up_challenges, OTP
                    # — в outbound_max_messages. Достаём их напрямую.
                    challenge_id = _peek_latest_challenge_id(worker_id, "open")
                    assert challenge_id, (
                        "503 from issue and no challenge in DB; cannot proceed"
                    )
                    logger.info(
                        "Issue returned 503 (Max unreachable for fake_max_user_id); "
                        "fetched challenge_id=%s directly from auth_db",
                        challenge_id,
                    )

                # Step-up: читаем OTP из outbox (postgres_notification).
                command_code = _wait_for_otp(worker_id, purpose="otp_command")
                logger.info("Got command OTP code for user_id=%s", worker_id)

                # Step-up: POST /commands/confirm → 201 + command body.
                confirm = worker_session.post(
                    f"{GATEWAY_URL}/api/v1/locker/{locker_id}/commands/confirm",
                    json={"challenge_id": challenge_id, "code": command_code},
                    timeout=30,
                )
                assert confirm.status_code == 201, (
                    f"expected 201 from confirm, got {confirm.status_code} "
                    f"{confirm.text}"
                )
                command = confirm.json()
                assert command.get("id"), f"command id missing: {command}"
                assert command.get("type") == "open", (
                    f"command type mismatch: {command.get('type')}"
                )
                command_id = command["id"]
                logger.info("Command enqueued via confirm: id=%s", command_id)

                # Ждём, пока релейный двойник выполнит open и history
                # зафиксирует executed.
                def open_executed() -> bool:
                    items = gateway.get_command_history(
                        locker_id=locker_id,
                        command_type="open",
                        status="executed",
                        limit=50,
                    )
                    return any(item.get("command_id") == command_id for item in items)

                wait_for(
                    open_executed,
                    timeout=_COMMAND_TIMEOUT_S,
                    interval=2,
                    message=(
                        f"command {command_id} did not reach executed status"
                    ),
                )
                logger.info("Command %s executed end-to-end", command_id)
        finally:
            gateway.delete_locker(locker_id)


    def test_close_skips_step_up_even_with_2fa(
        self,
        gateway: GatewayClient,
        worker_2fa: dict,
    ):
        """
        close ∉ OTP_REQUIRED_COMMANDS → даже с require_2fa=true POST /commands
        возвращает 201 напрямую, без challenge. Это явный opt-out по дизайну:
        принудительное закрытие должно идти без задержки 2FA.
        """
        worker_id = worker_2fa["id"]
        worker_session = worker_2fa["session"]

        relay_id = _random_relay_id()
        locker = _create_mqtt_locker(gateway, relay_id)
        locker_id = locker["id"]

        try:
            with RelayRunner(relay_id) as runner:
                runner.publish_stat()
                grant_locker_access(gateway, worker_id, locker_id)

                wait_for(
                    lambda: gateway.get_connectivity(locker_id) == "online",
                    timeout=_ONLINE_TIMEOUT_S,
                    interval=2,
                )

                # close команды должна идти напрямую — 201, без 202.
                resp = worker_session.post(
                    f"{GATEWAY_URL}/api/v1/locker/{locker_id}/commands",
                    json={"type": "close", "parameters": {}, "max_retries": 3},
                    timeout=30,
                )
                assert resp.status_code == 201, (
                    f"close must skip step-up (201), got {resp.status_code} "
                    f"{resp.text}"
                )
                command = resp.json()
                assert command.get("type") == "close"
                assert command.get("id"), f"close command id missing: {command}"
                logger.info(
                    "close command bypassed step-up for 2FA worker: id=%s",
                    command["id"],
                )
        finally:
            gateway.delete_locker(locker_id)
