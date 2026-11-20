#!/usr/bin/env python3
"""Полу-ручной end-to-end тест доставки OTP через РЕАЛЬНЫЙ Max-бот.

Прогоняет всю цепочку Max 2FA против живого Max API и реального телефона
с установленным Max. Скрипт драйвит автоматизируемые шаги и паузится на
двух действиях, которые может выполнить только человек: нажать «Начать»
в боте и прочитать пришедший код.

Цепочка:
  0. проверка токена бота            GET  {MAX_BOT_API_URL}/me
  1. вход админом (require_2fa=false) POST /api/v1/auth/login
  2. создание тестового юзера         POST /api/v1/users   -> invite-ссылка
  3. включение 2FA юзеру               POST /api/v1/user/{id}/enable-2fa -> invite-ссылка
  4. [человек] открыть ссылку в Max, нажать «Начать»
  5. ожидание привязки                 SELECT users.max_user_id (poller ловит bot_started)
  6. логин -> реальная отправка OTP    POST /api/v1/auth/login -> "OTP has been sent"
  7. [человек] прочитать код в Max
  8. ввод кода -> verify               POST /api/v1/auth/verify -> сессия выдана

Это ОПЕРАТОРСКИЙ инструмент, а не автотест: требует живого Max-аккаунта и
ручного ввода, поэтому в CI не запускается. Запуск — см. README.md.

Замечание про шаг 5: единственное обращение к БД напрямую — это ЧТЕНИЕ
users.max_user_id, чтобы без спама задетектить, что bot_started долетел
(привязка). Включение 2FA идёт через API (POST .../enable-2fa), которое
при непривязанном пользователе возвращает свежую invite-ссылку.
"""
from __future__ import annotations

import os
import sys
import time
from typing import NoReturn

import psycopg2
import requests

# ── Конфигурация из окружения ───────────────────────────────────────────────

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://apache_gateway").rstrip("/")
ADMIN_PHONE = os.getenv("ADMIN_PHONE", "+79000000000")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")

DB_HOST = os.getenv("DB_HOST", "postgres_user")
DB_PORT = int(os.getenv("DB_PORT", "5432"))
DB_USER = os.getenv("DB_USER", "admin")
DB_NAME = os.getenv("DB_NAME", "scud_users")

MAX_BOT_API_URL = os.getenv("MAX_BOT_API_URL", "https://platform-api.max.ru").rstrip("/")
MAX_BOT_USERNAME = os.getenv("MAX_BOT_USERNAME", "")

TEST_PASSWORD = os.getenv("TEST_PASSWORD", "maxotp-realtest-pass1")
TEST_FULL_NAME = os.getenv("TEST_FULL_NAME", "Max OTP Realtest")
BIND_TIMEOUT_S = int(os.getenv("BIND_TIMEOUT_S", "300"))

OTP_BINDING_REQUIRED = "max-binding-required"


def read_secret(name: str, env_var: str, default: str = "") -> str:
    """Прочитать секрет из /run/secrets/<name>, затем из env, затем default."""
    path = f"/run/secrets/{name}"
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    return os.getenv(env_var, default)


# ── Вывод в консоль ─────────────────────────────────────────────────────────

def section(title: str) -> None:
    print(f"\n\033[1;36m== {title} ==\033[0m")


def ok(msg: str) -> None:
    print(f"  \033[32m✓\033[0m {msg}")


def warn(msg: str) -> None:
    print(f"  \033[33m!\033[0m {msg}")


def fail(msg: str) -> None:
    print(f"  \033[31m✗ {msg}\033[0m")


def die(msg: str) -> NoReturn:
    fail(msg)
    sys.exit(1)


def ask(msg: str) -> str:
    return input(f"\033[1;35m? {msg}\033[0m ").strip()


# ── Шаги ─────────────────────────────────────────────────────────────────────

def db_connect(password: str):
    return psycopg2.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER,
        password=password, dbname=DB_NAME,
    )


def probe_token(token: str) -> None:
    """GET /me — ранняя проверка, что токен бота вообще валиден (best-effort)."""
    section("0. Проверка токена Max-бота")
    try:
        resp = requests.get(
            f"{MAX_BOT_API_URL}/me",
            headers={"Authorization": token},
            timeout=15,
        )
    except requests.RequestException as exc:
        warn(f"Не удалось обратиться к {MAX_BOT_API_URL}/me: {exc}")
        warn("Пропускаю проверку — настоящая валидация будет при отправке OTP.")
        return
    if resp.status_code != 200:
        warn(f"GET /me вернул {resp.status_code}: {resp.text[:200]}")
        warn("Возможно, эндпоинт /me не поддерживается — продолжаю.")
        return
    info = resp.json()
    name = info.get("name") or info.get("first_name") or "?"
    username = info.get("username") or "?"
    ok(f"Токен валиден. Бот: {name} (@{username})")
    global MAX_BOT_USERNAME
    if not MAX_BOT_USERNAME and username != "?":
        MAX_BOT_USERNAME = username
        ok(f"MAX_BOT_USERNAME подхвачен из /me: @{username}")


def admin_login() -> requests.Session:
    section("1. Вход администратором")
    session = requests.Session()
    resp = session.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": ADMIN_PHONE, "password": ADMIN_PASSWORD},
        timeout=30,
    )
    if resp.status_code != 200:
        die(f"Логин админа провалился: {resp.status_code} {resp.text[:300]}")
    if "access_token" not in session.cookies:
        die(f"Логин админа вернул 200, но без cookie. Тело: {resp.text[:300]} "
            f"(возможно, у админа включена 2FA — этого быть не должно)")
    ok(f"Админ {ADMIN_PHONE} вошёл, сессия получена")
    return session


def create_test_user(session: requests.Session) -> dict:
    section("2. Создание тестового пользователя")
    phone = "+7" + str(int(time.time()) % 10000000000).zfill(10)
    resp = session.post(
        f"{GATEWAY_URL}/api/v1/users",
        json={
            "phone": phone,
            "full_name": TEST_FULL_NAME,
            "password": TEST_PASSWORD,
            "role": "worker",
        },
        timeout=30,
    )
    if resp.status_code != 201:
        die(f"Создание пользователя провалилось: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    user_id = body["id"]
    ok(f"Пользователь создан: id={user_id}, phone={phone}")

    link = body.get("max_invite_link")
    token = body.get("max_invite_token")
    if not link and token and MAX_BOT_USERNAME:
        link = f"https://max.ru/{MAX_BOT_USERNAME}?start={token}"
    if not link:
        warn("max_invite_link не вернулся и собрать его не из чего "
             "(пустой MAX_BOT_USERNAME). invite-токен:")
        print(f"      {token}")
        die("Задай MAX_BOT_USERNAME и перезапусти, либо собери ссылку вручную.")
    return {"id": user_id, "phone": phone, "password": TEST_PASSWORD,
            "invite_link": link, "invite_token": token}


def enable_2fa(session: requests.Session, user_id: int) -> str | None:
    """Включить 2FA через API. Вернуть invite-ссылку из ответа (или None, если привязан)."""
    section("3. Включение 2FA через API")
    resp = session.post(f"{GATEWAY_URL}/api/v1/user/{user_id}/enable-2fa", json={}, timeout=30)
    if resp.status_code == 204:
        ok("2FA включена; пользователь уже привязан к Max")
        return None
    if resp.status_code == 200:
        body = resp.json()
        ok("2FA включена; пользователь не привязан — получена invite-ссылка")
        return body.get("max_invite_link")
    die(f"enable-2fa вернул {resp.status_code}: {resp.text[:300]}")


def show_invite(user: dict) -> None:
    section("4. Привязка Max-аккаунта (РУЧНОЙ ШАГ)")
    print("  Открой ссылку в приложении Max на телефоне и нажми «Начать»:\n")
    print(f"      \033[1;34m{user['invite_link']}\033[0m\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(user["invite_link"])
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception:
        # QR — nice-to-have: без него оператор открывает ссылку вручную.
        warn("Не удалось отрисовать QR — открой ссылку вручную.")
    ask("Нажми Enter ПОСЛЕ того, как нажал «Начать» в боте...")


def wait_for_binding(db_password: str, user_id: int) -> int:
    section("5. Ожидание привязки (poller ловит bot_started)")
    conn = db_connect(db_password)
    deadline = time.time() + BIND_TIMEOUT_S
    try:
        while time.time() < deadline:
            with conn, conn.cursor() as cur:
                cur.execute("SELECT max_user_id FROM users WHERE id = %s", (user_id,))
                row = cur.fetchone()
            max_user_id = row[0] if row else None
            if max_user_id is not None:
                ok(f"Привязка готова: users.max_user_id={max_user_id}")
                return max_user_id
            remaining = int(deadline - time.time())
            print(f"  …жду bot_started, осталось {remaining}s "
                  f"(если зависло — `docker compose logs -f notification_api`)",
                  end="\r")
            time.sleep(3)
    finally:
        conn.close()
    print()
    die(f"Привязка не произошла за {BIND_TIMEOUT_S}s. Проверь, что бот запущен "
        f"(MAX_BOT_TOKEN задан) и поллер ловит updates.")


def trigger_otp(user: dict) -> None:
    section("6. Логин → реальная отправка OTP в Max")
    session = requests.Session()
    resp = session.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": user["phone"], "password": user["password"]},
        timeout=30,
    )
    if resp.status_code == 200 and resp.json().get("message") == "OTP has been sent":
        ok("login вернул «OTP has been sent» — код ушёл в Max")
        return
    if resp.status_code == 401 and OTP_BINDING_REQUIRED in resp.text:
        die("login вернул max-binding-required — привязка не подхватилась "
            "(max_user_id пуст на момент логина).")
    if resp.status_code == 503:
        die(f"login вернул 503 — Max API/notification_api недоступны: {resp.text[:300]}")
    die(f"Неожиданный ответ login: {resp.status_code} {resp.text[:300]}")


def verify_otp(user: dict) -> None:
    section("7-8. Ввод кода из Max → verify")
    for attempt in range(1, 4):
        code = ask(f"Введи 6-значный код из Max (попытка {attempt}/3):")
        session = requests.Session()
        resp = session.post(
            f"{GATEWAY_URL}/api/v1/auth/verify",
            json={"phone": user["phone"], "code": code},
            timeout=30,
        )
        if resp.status_code == 200 and "access_token" in session.cookies:
            ok("verify вернул 200 + сессионный cookie — вход подтверждён!")
            return
        fail(f"verify не прошёл: {resp.status_code} {resp.text[:200]}")
    die("Исчерпаны попытки ввода кода.")


def cleanup(session: requests.Session, user_id: int) -> None:
    section("Очистка")
    answer = ask(f"Деактивировать тестового пользователя id={user_id}? [y/N]:")
    if answer.lower() in ("y", "yes", "д", "да"):
        resp = session.delete(f"{GATEWAY_URL}/api/v1/user/{user_id}", timeout=30)
        if resp.status_code in (204, 404):
            ok(f"Пользователь id={user_id} деактивирован")
        else:
            warn(f"Не удалось деактивировать: {resp.status_code} {resp.text[:200]}")
    else:
        ok(f"Пользователь id={user_id} оставлен как есть")


def main() -> None:
    token = read_secret("max_bot_token", "MAX_BOT_TOKEN")
    if not token:
        die("MAX_BOT_TOKEN пуст. Положи токен бота в backend/secrets/max_bot_token "
            "(из @MasterBot) и перезапусти.")
    db_password = read_secret("postgres_user_password", "DB_PASSWORD", "password")

    print("\033[1mMax OTP — полу-ручной end-to-end тест\033[0m")
    print(f"Gateway: {GATEWAY_URL}  |  Max API: {MAX_BOT_API_URL}")

    probe_token(token)
    admin = admin_login()
    user = create_test_user(admin)
    invite_link = enable_2fa(admin, user["id"])
    if invite_link:
        user["invite_link"] = invite_link
    show_invite(user)
    wait_for_binding(db_password, user["id"])
    trigger_otp(user)
    verify_otp(user)

    section("РЕЗУЛЬТАТ")
    ok("Полная цепочка Max 2FA прошла: invite → bot_started → bind → OTP → verify ✅")
    cleanup(admin, user["id"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nПрервано оператором.")
        sys.exit(130)
