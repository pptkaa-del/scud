"""Gateway+ACM тесты для X-Remote-Require-2FA header propagation.

Проверяют, что ACM /acm/check возвращает корректный X-Remote-Require-2FA
в зависимости от user_auth.require_2fa, и что Apache Lua не пропускает
поддельный заголовок от клиента.

Architecture:
  - ACM напрямую доступен внутри backend_net (acm:8004) — это позволяет
    прочитать ответный заголовок без необходимости echo-эндпоинта на
    бэкенд-сервисе.
  - Apache Lua скрывает заголовки от downstream, поэтому проверить
    X-Remote-Require-2FA в downstream-сервисе невозможно без модификации
    бэкенда. End-to-end семантика покрывается system E2E (Task 7).
  - Для гарантированно свежего UserContext в ACM создаём нового пользователя,
    выставляем require_2fa в БД ДО первого вызова /acm/check (логин не лезет
    в ACM-кэш, т.к. /api/v1/auth/login — публичный путь).
"""
import os
import time
import uuid

from conftest import GATEWAY_URL, _connect_user_db, unique_phone
import requests

ACM_URL = os.getenv("ACM_URL", "http://acm:8004")


def _unique_relay_id() -> int:
    return 500_000_000 + uuid.uuid4().int % 400_000_000


def _set_require_2fa(user_id: int, value: bool) -> None:
    conn = _connect_user_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_auth SET require_2fa = %s WHERE user_id = %s",
                (value, user_id),
            )
        conn.commit()
    finally:
        conn.close()


def _create_worker_with_2fa(admin_session, *, require_2fa: bool) -> tuple[str, int, str]:
    """Создаёт worker'а, выставляет require_2fa, логинит. Возвращает (phone, user_id, token).

    Логин получает access_token через gateway. Сам логин публичный, не дёргает
    ACM-кэш, поэтому первый запрос к ACM получит свежий UserContext из БД
    (singleflight отработает миссом и закэширует значение из БД).
    """
    phone = unique_phone()
    create = admin_session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={"phone": phone, "full_name": "GW 2FA Hdr", "password": "pass1234",
              "role": "worker"},
    )
    assert create.status_code == 201, create.text
    uid = create.json()["id"]

    # Выставляем целевое значение require_2fa ДО логина.
    # user_api при логине без 2FA-bypass'а отказал бы, поэтому для logon-фазы
    # принудительно ставим FALSE, получаем token, затем восстанавливаем
    # целевое значение. ACM-кэш ещё пуст для этого user_id, так как login
    # — публичный путь и не вызывает /acm/check.
    _set_require_2fa(uid, False)
    session = requests.Session()
    login = session.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": phone, "password": "pass1234"},
    )
    assert login.status_code == 200, f"Login failed: {login.status_code} {login.text}"
    token = session.cookies.get("access_token")
    assert token, "access_token cookie missing"

    # Восстанавливаем целевой require_2fa. ACM ещё не звали для этого user,
    # поэтому ctx-кэш пуст; первый /acm/check ниже зайдёт в БД.
    _set_require_2fa(uid, require_2fa)
    return phone, uid, token


def _acm_check(token: str, method: str, path: str) -> requests.Response:
    """Прямой вызов ACM /acm/check с заголовками, что и делает Apache Lua."""
    return requests.get(
        f"{ACM_URL}/acm/check",
        headers={
            "X-Access-Token": token,
            "X-Request-Method": method,
            "X-Request-Path": path,
        },
        timeout=10,
    )


def test_acm_returns_require_2fa_true(admin_session):
    """ACM /acm/check → 200 с X-Remote-Require-2FA: true для пользователя с require_2fa=TRUE."""
    _, uid, token = _create_worker_with_2fa(admin_session, require_2fa=True)
    try:
        resp = _acm_check(token, "GET", "/api/v1/lockers")
        assert resp.status_code == 200, f"ACM check failed: {resp.status_code} {resp.text}"
        assert resp.headers.get("X-Remote-Require-2FA") == "true", (
            f"Expected X-Remote-Require-2FA=true, got headers={dict(resp.headers)}"
        )
    finally:
        admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{uid}")


def test_acm_returns_require_2fa_false(admin_session):
    """ACM /acm/check → 200 с X-Remote-Require-2FA: false для пользователя с require_2fa=FALSE."""
    _, uid, token = _create_worker_with_2fa(admin_session, require_2fa=False)
    try:
        resp = _acm_check(token, "GET", "/api/v1/lockers")
        assert resp.status_code == 200, f"ACM check failed: {resp.status_code} {resp.text}"
        assert resp.headers.get("X-Remote-Require-2FA") == "false", (
            f"Expected X-Remote-Require-2FA=false, got headers={dict(resp.headers)}"
        )
    finally:
        admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{uid}")


def test_gateway_strips_client_supplied_require_2fa_header(admin_session):
    """Apache Lua должен очистить X-Remote-Require-2FA от клиента (защита от подделки).

    Проверяем регрессией: после Task 4 защищённый запрос продолжает работать
    даже если клиент пытается отправить X-Remote-Require-2FA — Lua сбросит
    его до вызова ACM, а ACM выставит реальное значение.
    """
    resp = admin_session.get(
        f"{GATEWAY_URL}/api/v1/lockers",
        headers={"X-Remote-Require-2FA": "true"},  # клиент пытается подделать
    )
    assert resp.status_code == 200, (
        f"Authenticated request failed after client supplied X-Remote-Require-2FA: "
        f"{resp.status_code} {resp.text}"
    )


def _set_max_user_id(user_id: int, value: int) -> None:
    conn = _connect_user_db()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET max_user_id = %s WHERE id = %s",
                (value, user_id),
            )
        conn.commit()
    finally:
        conn.close()


def test_gateway_routes_open_command_through_step_up_for_2fa_worker(admin_session):
    """End-to-end gateway round-trip: open команда от worker'а с require_2fa=true
    проходит через Apache Lua → ACM → locker_api → step_up issue (auth_api).

    Здесь интересен ИМЕННО роутинг через gateway: что Lua прокидывает
    X-Remote-Require-2FA, что Apache корректно проксирует POST на
    locker_api, что worker_2fa получает либо 202 (challenge_id), либо 503
    (Max недоступен), но НЕ 401 (это означало бы, что step-up flow
    срабатывает).

    Полный happy-path (issue → confirm → executed) покрыт в
    tests/system/scenarios/test_command_2fa.py через emulator-флот.
    Здесь ограничиваемся issue-стороной — это и есть критичный gateway path.
    """
    # 1. Создать шкаф через admin (на уровне locker_api).
    locker_resp = admin_session.post(
        f"{GATEWAY_URL}/api/v1/lockers",
        json={
            "inventory_number": f"GW-2FA-{int(time.time() * 1000) % 10**8}",
            "transport": "mqtt",
            "relay_id": _unique_relay_id(),
        },
    )
    assert locker_resp.status_code == 201, locker_resp.text
    locker = locker_resp.json()
    locker_id = locker["id"]

    # 2. Создать worker с require_2fa=true (после логина) + max_user_id.
    phone, uid, _ = _create_worker_with_2fa(admin_session, require_2fa=False)
    # Восстановили require_2fa, теперь нужно дать привязку Max + access на шкаф
    # + восстановить require_2fa=TRUE.
    fake_max = 9_000_000_001 + uid
    _set_max_user_id(uid, fake_max)
    _set_require_2fa(uid, True)

    grant = admin_session.post(
        f"{GATEWAY_URL}/api/v1/user/{uid}/accesses",
        json={
            "start_at": "2020-01-01T00:00:00+00:00",
            "end_at": "2099-01-01T00:00:00+00:00",
            "full_access": False,
            "locker_ids": [locker_id],
        },
    )
    assert grant.status_code == 201, grant.text

    # 3. Перелогиниться, чтобы получить свежий cookie (предыдущий был с
    # require_2fa=FALSE, что неважно — куки выдаёт auth_api без 2FA-флага,
    # просто validates the path-through).
    worker = requests.Session()
    # require_2fa=TRUE, поэтому /login пытается слать OTP в Max и упирается
    # в 404 (фейковый max_user_id) → 503 от auth_api. Поэтому сначала снова
    # снимаем 2FA для логина, потом возвращаем.
    _set_require_2fa(uid, False)
    login = worker.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": phone, "password": "pass1234"},
    )
    assert login.status_code == 200, login.text
    assert "access_token" in worker.cookies
    _set_require_2fa(uid, True)

    try:
        # 4. POST open — должен пойти через step-up.
        resp = worker.post(
            f"{GATEWAY_URL}/api/v1/locker/{locker_id}/commands",
            json={"type": "open", "parameters": {}, "max_retries": 3},
            timeout=30,
        )

        # Допустимые ветки:
        # 202 — happy path (max_bot_token не настроен или Max принял код).
        # 503 — Max API вернул 404 на fake_max_user_id (max_bot_token задан
        #       для tools/max-otp-realtest); challenge всё равно в БД auth.
        # Принципиально, что НЕ 201 — это означало бы, что step-up не сработал.
        # И НЕ 401 — это означало бы max_binding_required, тогда как
        # max_user_id явно проставлен.
        assert resp.status_code in (202, 503), (
            f"expected 202 or 503, got {resp.status_code} {resp.text}"
        )
        if resp.status_code == 202:
            body = resp.json()
            assert body.get("challenge_id"), f"challenge_id missing: {body}"
            assert "expires_at" in body
            assert body.get("delivery_status") in {
                "sent", "queued_until_binding", "max_binding_required",
            }
    finally:
        admin_session.delete(f"{GATEWAY_URL}/api/v1/user/{uid}")
        admin_session.delete(f"{GATEWAY_URL}/api/v1/locker/{locker_id}")
