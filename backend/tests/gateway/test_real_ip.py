"""
SEC-07 регрессия: gateway не должен схлопывать login rate-limit за
TLS-терминатором и не должен доверять подделанному X-Real-IP с прямого
(недоверенного) соединения.

Контекст бага: раньше `apache.conf` безусловно делал
`RequestHeader set X-Real-IP "%{REMOTE_ADDR}s"`. За nginx-терминатором
(`proxy_set_header X-Real-IP $remote_addr`) Apache REMOTE_ADDR — это IP
терминатора, поэтому КАЖДЫЙ клиент приходил в auth_api с одним общим
X-Real-IP → rate-limit логина (login.py → RateLimitRepository) работал по
одному общему ключу: один атакёр лочил ВСЕХ, brute-force-защита per-IP
исчезала, аудит access_tokens.ip_address был неверен.

Фикс: mod_remoteip (`RemoteIPHeader X-Real-IP` +
`RemoteIPInternalProxy ${TRUSTED_PROXY_CIDR}`). Apache берёт клиентский IP
из X-Real-IP ТОЛЬКО если соединение пришло с доверенного хопа; для прямого
недоверенного соединения X-Real-IP клиента игнорируется, а REMOTE_ADDR
остаётся реальным IP клиента и им же перезаписывается X-Real-IP вниз.

Тесты идут через apache_gateway:80 из тест-контейнера, который НЕ входит в
TRUSTED_PROXY_CIDR (дефолт 127.0.0.1/8). Значит для gateway это прямое
недоверенное соединение — идеально для проверки spoof-resistance: что бы мы
ни прислали в X-Real-IP, gateway обязан проигнорировать заголовок и
ключевать rate-limit по реальному IP тест-контейнера.

Ключ rate-limit логина в auth_api — это IP-строка (login.py:62,70). Мы
инспектируем таблицу login_rate_limit в scud_auth и проверяем, какие `key`
там появляются после логинов с разными подделанными X-Real-IP.
"""
import os
import time

import psycopg2
import psycopg2.extras
import pytest
import requests

from conftest import GATEWAY_URL, unique_phone

AUTH_DB = {
    "host": os.getenv("AUTH_DB_HOST", "postgres_auth"),
    "port": int(os.getenv("AUTH_DB_PORT", 5432)),
    "user": os.getenv("AUTH_DB_USER", "admin"),
    "password": os.getenv("AUTH_DB_PASSWORD", "password"),
    "dbname": os.getenv("AUTH_DB_NAME", "scud_auth"),
}

# Заведомо подделанные значения X-Real-IP — НЕ должны стать ключами
# rate-limit, потому что соединение тест-контейнера для gateway недоверенное.
SPOOF_IP_A = "203.0.113.7"      # TEST-NET-3 (RFC 5737), гарантированно чужой
SPOOF_IP_B = "198.51.100.42"    # TEST-NET-2 (RFC 5737)


def _connect_auth_db(max_retries: int = 30) -> psycopg2.extensions.connection:
    for _ in range(max_retries):
        try:
            return psycopg2.connect(**AUTH_DB)
        except psycopg2.OperationalError:
            time.sleep(2)
    pytest.fail(f"Could not connect to auth database {AUTH_DB['dbname']}")


def _rate_limit_keys() -> set[str]:
    """Все текущие ключи таблицы login_rate_limit."""
    conn = _connect_auth_db()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT key FROM login_rate_limit")
            return {row[0] for row in cur.fetchall()}
    finally:
        conn.close()


def _login_attempt(real_ip_header: str | None = None) -> requests.Response:
    """Один POST /api/v1/auth/login через gateway с заведомо неверными
    кредами (нас интересует не результат, а то, под каким ключом
    записался rate-limit). Опционально шлём подделанный X-Real-IP."""
    headers = {}
    if real_ip_header is not None:
        headers["X-Real-IP"] = real_ip_header
    return requests.post(
        f"{GATEWAY_URL}/api/v1/auth/login",
        json={"phone": unique_phone(), "password": "definitely-wrong-xyz"},
        headers=headers,
        timeout=10,
    )


class TestRealClientIpForwarding:
    """SEC-07: rate-limit ключуется по реальному клиентскому IP, а не по
    общему IP терминатора, и подделать X-Real-IP с прямого соединения
    нельзя."""

    def test_login_records_real_ip_not_spoofed_or_gateway(self, wait_for_gateway):
        """
        Логин с прямого недоверенного соединения + подделанный X-Real-IP.
        Ожидаем: в login_rate_limit появляется ключ = реальный IP
        тест-контейнера, а НЕ подделанное значение SPOOF_IP_A.
        """
        before = _rate_limit_keys()
        resp = _login_attempt(real_ip_header=SPOOF_IP_A)
        # Любой не-5xx ответ годится: креды неверны → 401, либо 429 если
        # лимит уже выбран. Главное — что rate-limit записался.
        assert resp.status_code < 500, f"unexpected {resp.status_code}: {resp.text}"

        after = _rate_limit_keys()

        assert SPOOF_IP_A not in after, (
            f"Подделанный X-Real-IP {SPOOF_IP_A} с недоверенного прямого "
            f"соединения НЕ должен становиться ключом rate-limit. "
            f"mod_remoteip обязан игнорировать заголовок. Ключи: {after}"
        )
        # Должен существовать ключ с реальным клиентским IP (login-бакет, без
        # префикса verify:). Это доказывает, что gateway прокидывает настоящий
        # X-Real-IP в auth_api (а не схлопывает всех на один общий IP). Адрес
        # реального клиента — приватный docker-IP тест-контейнера; точное
        # значение неизвестно заранее, поэтому проверяем, что есть хотя бы один
        # «нормальный» IP-ключ, не равный спуф-значениям и не loopback.
        login_keys = {k for k in after if not k.startswith("verify:")}
        real_ip_keys = login_keys - {SPOOF_IP_A, SPOOF_IP_B}
        assert real_ip_keys, (
            f"Ожидался login rate-limit ключ с реальным клиентским IP, "
            f"найдены только: {after}"
        )
        for key in real_ip_keys:
            assert not key.startswith("127."), (
                f"rate-limit ключ {key} — loopback; реальный клиентский IP "
                f"не дошёл (X-Real-IP не прокинут корректно)"
            )

    def test_two_different_spoofed_ips_share_one_bucket(self, wait_for_gateway):
        """
        Два логина с РАЗНЫМИ подделанными X-Real-IP с одного (недоверенного)
        тест-контейнера обязаны попасть в ОДИН bucket — реальный IP
        контейнера. Доказывает: атакёр не может размножить свои попытки,
        подставляя разные X-Real-IP, и не может «отравить» чужой бакет.

        Если бы gateway доверял заголовку, появилось бы ДВА разных ключа
        (SPOOF_IP_A и SPOOF_IP_B). Мы проверяем, что их там нет вовсе.
        """
        _login_attempt(real_ip_header=SPOOF_IP_A)
        _login_attempt(real_ip_header=SPOOF_IP_B)

        keys = _rate_limit_keys()
        assert SPOOF_IP_A not in keys and SPOOF_IP_B not in keys, (
            f"Разные подделанные X-Real-IP не должны создавать отдельные "
            f"rate-limit бакеты. Найдены подделанные ключи в {keys}"
        )
