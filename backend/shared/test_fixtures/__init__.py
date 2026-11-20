"""
Общие хелперы для интеграционных тестов сервисов backend.

Модуль предоставляет параметризованные функции и класс HTTP-клиента, которые
дублировались по conftest.py каждого сервиса. Сервисные conftest.py должны
импортировать эти хелперы и оборачивать их в pytest-фикстуры с локальными
параметрами (хост БД, пробная таблица, путь probe-эндпоинта и т.д.).

Импорт: `from shared.test_fixtures import wait_for_db, wait_for_api, APIClient`

Зависимости (psycopg2, requests) уже устанавливаются Dockerfile-ами всех
сервисов из репозитория Astra Linux.
"""
import time
from typing import Any

import psycopg2
import requests


def wait_for_db(
    host: str,
    port: int,
    user: str,
    password: str,
    dbname: str,
    probe_table: str,
    max_retries: int = 30,
    retry_delay: float = 1.0,
) -> None:
    """
    Ожидает готовности PostgreSQL: подключается, дожидается появления
    `probe_table` (запросом `SELECT 1 FROM <probe_table> LIMIT 1`).

    Бросает `RuntimeError`, если за `max_retries` итераций соединение не
    установлено или таблица не появилась (миграции не успели отработать).
    """
    conn = None
    for _ in range(max_retries):
        try:
            conn = psycopg2.connect(
                host=host, port=port, user=user, password=password, dbname=dbname
            )
            break
        except psycopg2.OperationalError:
            time.sleep(retry_delay)

    if conn is None:
        raise RuntimeError(f"Could not connect to database {dbname} at {host}:{port}")

    try:
        for _ in range(max_retries):
            try:
                with conn.cursor() as cur:
                    cur.execute(f"SELECT 1 FROM {probe_table} LIMIT 1")
                return
            except psycopg2.Error:
                conn.rollback()
                time.sleep(retry_delay)
        raise RuntimeError(
            f"Database {dbname}: table '{probe_table}' not ready after migrations"
        )
    finally:
        conn.close()


def wait_for_api(
    base_url: str,
    probe_path: str,
    headers: dict[str, str] | None = None,
    timeout: int = 90,
    accepted_statuses: tuple[int, ...] = (200, 401),
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
) -> None:
    """
    Дёргает `<base_url><probe_path>` каждую секунду, пока ответ не вернёт один
    из `accepted_statuses` или не истечёт `timeout` (в секундах).

    `method` поддерживает GET и POST — для тех сервисов (auth_api), у которых
    health-probe — это POST с JSON-телом.
    """
    url = f"{base_url}{probe_path}"
    deadline = time.time() + timeout
    method_upper = method.upper()
    while time.time() < deadline:
        try:
            if method_upper == "POST":
                response = requests.post(url, headers=headers, json=json_body, timeout=5)
            else:
                response = requests.get(url, headers=headers, timeout=5)
            if response.status_code in accepted_statuses:
                return
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(1)
    raise RuntimeError(f"API at {base_url} not reachable after {timeout}s")


class APIClient:
    """
    Тонкая обёртка над `requests.Session` с базовым URL и набором заголовков
    по умолчанию.

    Методы `get`, `post`, `put`, `patch`, `delete` принимают `path` и любые
    `**kwargs` для `requests.<method>`. Заголовки из вызова мерджатся поверх
    дефолтных. Для POST/PUT/PATCH автоматически проставляется
    `Content-Type: application/json`, кроме случаев, когда передан `files`
    (multipart).
    """

    def __init__(self, base_url: str, default_headers: dict[str, str] | None = None) -> None:
        self.base_url = base_url
        self.headers = dict(default_headers or {})
        self.session = requests.Session()

    def _merge_headers(self, kwargs: dict[str, Any], json_body: bool) -> dict[str, str]:
        headers = kwargs.pop("headers", {}) or {}
        merged = {**self.headers, **headers}
        if json_body and "files" not in kwargs and "Content-Type" not in merged:
            merged["Content-Type"] = "application/json"
        return merged

    def get(self, path: str, **kwargs: Any) -> requests.Response:
        headers = self._merge_headers(kwargs, json_body=False)
        return self.session.get(f"{self.base_url}{path}", headers=headers, **kwargs)

    def post(self, path: str, **kwargs: Any) -> requests.Response:
        headers = self._merge_headers(kwargs, json_body=True)
        return self.session.post(f"{self.base_url}{path}", headers=headers, **kwargs)

    def put(self, path: str, **kwargs: Any) -> requests.Response:
        headers = self._merge_headers(kwargs, json_body=True)
        return self.session.put(f"{self.base_url}{path}", headers=headers, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> requests.Response:
        headers = self._merge_headers(kwargs, json_body=True)
        return self.session.patch(f"{self.base_url}{path}", headers=headers, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> requests.Response:
        headers = self._merge_headers(kwargs, json_body=False)
        return self.session.delete(f"{self.base_url}{path}", headers=headers, **kwargs)
