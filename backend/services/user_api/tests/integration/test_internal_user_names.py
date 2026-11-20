"""Интеграционные тесты для GET /internal/users/names.

Internal-эндпоинт закрыт Apache снаружи (Require all denied), поэтому
тест обращается к user_api НАПРЯМУЮ по его порту (BASE_URL), как и
остальные internal-эндпоинты вызываются из backend_net.

Сидируем пользователей напрямую через psycopg2 (db_connection), затем
GET /internal/users/names?ids=... и проверяем, что map ФИО совпадает с
посеянными значениями и что несуществующие id опущены. Дополнительно —
что ФИО возвращается даже для деактивированного пользователя.
"""
import os
import time
from urllib.parse import urlencode

import requests

from shared.utils import body_sha256, compute_request_signature, signed_path
from tests.integration.conftest import BASE_URL

_NAMES_PATH = "/internal/users/names"
# Тот же ключ, что у user_api (docker-compose env). Если не задан — fail-open,
# user_api тоже пропускает unsigned (dev без .env).
_READ_KEY = os.getenv("INTERNAL_USER_READ_HMAC_KEY", "")


def _names_url(ids: str | None = None) -> tuple[str, dict]:
    """Построить URL + X-Internal-* подпись для GET /internal/users/names.

    Query-строка собирается через urlencode и идёт И в URL, И (свёрнутая
    через signed_path) в подпись — селектор ids= входит в канон (SEC-10).
    Возвращает (url, headers); headers пустой при fail-open (ключ не задан).
    """
    qs = urlencode({"ids": ids}) if ids is not None else ""
    url = f"{BASE_URL}{_NAMES_PATH}" + (f"?{qs}" if qs else "")
    if not _READ_KEY:
        return url, {}
    ts = str(int(time.time()))
    sig = compute_request_signature(
        timestamp=ts,
        method="GET",
        path=signed_path(_NAMES_PATH, qs),
        body_sha256_hex=body_sha256(b""),
        key=_READ_KEY.encode("utf-8"),
    )
    return url, {"X-Internal-Timestamp": ts, "X-Internal-Signature": sig}


def _seed_user(db_connection, phone: str, full_name: str, state: str = "active") -> int:
    """Создать пользователя напрямую в БД, вернуть его id."""
    with db_connection.cursor() as cur:
        cur.execute(
            """
            INSERT INTO users (phone, full_name, state, role)
            VALUES (%s, %s, %s, 'worker')
            RETURNING id
            """,
            (phone, full_name, state),
        )
        user_id = cur.fetchone()[0]
        db_connection.commit()
    return int(user_id)


def _unique_phone() -> str:
    return f"+7{str(int(time.time() * 1000000))[-10:]}"


def test_names_resolves_seeded_and_omits_missing(wait_for_api, db_connection):
    id_a = _seed_user(db_connection, _unique_phone(), "Иванов Иван")
    id_b = _seed_user(db_connection, _unique_phone(), "Петров Пётр")
    missing_id = 99999999  # заведомо несуществующий

    url, headers = _names_url(f"{id_a},{missing_id},{id_b}")
    response = requests.get(url, headers=headers)

    assert response.status_code == 200
    names = response.json()["names"]
    assert names[str(id_a)] == "Иванов Иван"
    assert names[str(id_b)] == "Петров Пётр"
    # Несуществующий id опущен.
    assert str(missing_id) not in names


def test_names_returns_name_for_deactivated_user(wait_for_api, db_connection):
    """ФИО должно отдаваться независимо от state (исторический отчёт)."""
    uid = _seed_user(db_connection, _unique_phone(), "Сидоров Сидор", state="deactivated")

    url, headers = _names_url(str(uid))
    response = requests.get(url, headers=headers)

    assert response.status_code == 200
    assert response.json()["names"][str(uid)] == "Сидоров Сидор"


def test_names_empty_ids_returns_empty_map(wait_for_api):
    url, headers = _names_url()
    response = requests.get(url, headers=headers)
    assert response.status_code == 200
    assert response.json() == {"names": {}}


def test_names_malformed_ids_returns_400(wait_for_api):
    url, headers = _names_url("a,b,c")
    response = requests.get(url, headers=headers)
    assert response.status_code == 400


def test_names_unsigned_returns_401_when_key_configured(wait_for_api):
    """Без подписи (когда ключ настроен) → 401 до обращения к БД (SEC-10)."""
    if not _READ_KEY:
        # В dev без ключа эндпойнт fail-open — нечего проверять.
        return
    url = f"{BASE_URL}{_NAMES_PATH}?{urlencode({'ids': '1'})}"
    response = requests.get(url)
    assert response.status_code == 401


def test_names_tampered_query_returns_401_when_key_configured(wait_for_api):
    """Подпись снята для ids=1,2, а запрос несёт ids=9,9 → 401 (SEC-10).

    Доказывает, что query теперь связан подписью: подменить селектор внутри
    replay-окна нельзя.
    """
    if not _READ_KEY:
        return
    # Подписываем канонический query для ids=1,2.
    _, headers = _names_url("1,2")
    # Но шлём другой query — verifier сворачивает уже его, подпись не сходится.
    tampered_url = f"{BASE_URL}{_NAMES_PATH}?{urlencode({'ids': '9,9'})}"
    response = requests.get(tampered_url, headers=headers)
    assert response.status_code == 401
