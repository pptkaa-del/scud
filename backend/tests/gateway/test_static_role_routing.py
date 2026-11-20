"""Интеграционные тесты ролевой раздачи статики через Apache gateway.

Требуют FRONTEND_PATH, указывающий на tests/gateway/fixtures/frontend.
requests не должен следовать редиректам там, где мы проверяем 302
(allow_redirects=False).
"""
import http.client
from urllib.parse import urlparse

import requests


def _raw_get(base_url: str, raw_path: str) -> tuple[int, str]:
    """GET по сырому пути через http.client — он НЕ нормализует path в request-line,
    в отличие от requests (тот схлопывает `..` на стороне клиента). Нужно, чтобы
    traversal реально дошёл до сервера и сработал path-safety фенс static_route."""
    netloc = urlparse(base_url).netloc or base_url
    conn = http.client.HTTPConnection(netloc, timeout=10)
    try:
        conn.request("GET", raw_path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        return resp.status, body
    finally:
        conn.close()


class TestUnauthenticatedAndPublic:
    def test_libs_always_public(self, wait_for_gateway, base_url):
        r = requests.get(f"{base_url}/libs/app.js", allow_redirects=False)
        assert r.status_code == 200
        assert "LIBS-JS" in r.text

    def test_login_page_public(self, wait_for_gateway, base_url):
        r = requests.get(f"{base_url}/login.html", allow_redirects=False)
        assert r.status_code == 200
        assert "LOGIN-PAGE" in r.text

    def test_public_asset_served_without_cookie(self, wait_for_gateway, base_url):
        r = requests.get(f"{base_url}/style.css", allow_redirects=False)
        assert r.status_code == 200
        assert "PUBLIC-CSS" in r.text

    def test_unauthenticated_navigation_redirects_to_login(self, wait_for_gateway, base_url):
        r = requests.get(f"{base_url}/main.html", allow_redirects=False,
                         headers={"Sec-Fetch-Mode": "navigate"})
        assert r.status_code == 302
        assert r.headers["Location"] == "/login.html"

    def test_unauthenticated_navigation_to_login_page_not_looped(self, wait_for_gateway, base_url):
        # Регресс: /login.html — навигация браузера (Sec-Fetch-Mode: navigate),
        # но БЕЗ исключения для самого /login.html правило "навигация → редирект
        # на /login.html" зацикливает редирект сам на себя (TOO_MANY_REDIRECTS).
        r = requests.get(f"{base_url}/login.html", allow_redirects=False,
                         headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
        assert r.status_code == 200
        assert "LOGIN-PAGE" in r.text

    def test_unauthenticated_navigation_to_root_still_redirects(self, wait_for_gateway, base_url):
        # Инвариант: фикс не должен расширяться на "/" — только /login.html
        # является публичной навигируемой страницей, "/" по-прежнему уходит на login.
        r = requests.get(f"{base_url}/", allow_redirects=False,
                         headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
        assert r.status_code == 302
        assert r.headers["Location"] == "/login.html"

    def test_traversal_rejected(self, wait_for_gateway, base_url):
        # Сырые dot-segment traversal'ы отвергаются фенсом static_route (400).
        # http.client не нормализует path — иначе requests схлопнул бы `..`.
        for bad in ["/../admin/main.html", "/%2e%2e/admin/main.html"]:
            status, body = _raw_get(base_url, bad)
            assert status == 400, f"{bad} -> {status}"
            assert "ADMIN-HOME" not in body
        # `//` Apache схлопывает (MergeSlashes) ДО Lua → путь становится безопасным;
        # инвариант: контент admin НИКОГДА не утекает, из какой бы формы ни пришёл путь.
        _status, body = _raw_get(base_url, "//admin/main.html")
        assert "ADMIN-HOME" not in body


class TestAuthenticatedRoleServing:
    def test_worker_root_redirects_to_worker_home(self, worker_session, base_url):
        session, _uid = worker_session
        r = session.get(f"{base_url}/", allow_redirects=False,
                        headers={"Sec-Fetch-Mode": "navigate"})
        assert r.status_code == 302
        assert r.headers["Location"] == "/main.html"

    def test_worker_main_served_from_worker_bundle(self, worker_session, base_url):
        session, _uid = worker_session
        r = session.get(f"{base_url}/main.html", allow_redirects=False)
        assert r.status_code == 200
        assert "WORKER-HOME" in r.text
        assert r.headers.get("Cache-Control") == "no-store"

    def test_admin_main_served_from_admin_bundle(self, admin_session, base_url):
        r = admin_session.get(f"{base_url}/main.html", allow_redirects=False)
        assert r.status_code == 200
        assert "ADMIN-HOME" in r.text

    def test_operator_main_served_from_operator_bundle(self, operator_session, base_url):
        session, _uid = operator_session
        r = session.get(f"{base_url}/main.html", allow_redirects=False)
        assert r.status_code == 200
        assert "OPERATOR-HOME" in r.text

    def test_worker_cannot_reach_admin_only_page(self, worker_session, base_url):
        session, _uid = worker_session
        r = session.get(f"{base_url}/users.html", allow_redirects=False)
        assert "ADMIN-USERS" not in r.text

    def test_worker_cannot_path_address_admin_folder(self, worker_session, base_url):
        session, _uid = worker_session
        r = session.get(f"{base_url}/admin/main.html", allow_redirects=False)
        assert "ADMIN-HOME" not in r.text

    def test_role_asset_no_store(self, worker_session, base_url):
        # §7.8: no-store вешается на ВСЕ ассеты роли, не только на HTML-shell —
        # иначе на общей рабочей станции кэш отдаст чужой ролевой бандл.
        session, _uid = worker_session
        r = session.get(f"{base_url}/style.css", allow_redirects=False)
        assert r.status_code == 200
        assert "WORKER-CSS" in r.text
        assert r.headers.get("Cache-Control") == "no-store"

    def test_role_page_keeps_security_headers(self, worker_session, base_url):
        # §7.5: внутренне переписанные (r.filename) файловые ответы роли сохраняют
        # security-заголовки vhost (CSP/nosniff) — Header always set к ним применяется.
        session, _uid = worker_session
        r = session.get(f"{base_url}/main.html", allow_redirects=False)
        assert r.status_code == 200
        assert "Content-Security-Policy" in r.headers
        assert r.headers.get("X-Content-Type-Options") == "nosniff"

    def test_api_path_unaffected(self, worker_session, base_url):
        session, _uid = worker_session
        r = session.get(f"{base_url}/api/v1/users", allow_redirects=False)
        assert r.status_code == 403


class TestHardening:
    def test_expired_cookie_treated_as_unauthenticated(self, wait_for_gateway, base_url):
        s = requests.Session()
        s.cookies.set("access_token", "definitelynotarealtoken123456789")
        r = s.get(f"{base_url}/main.html", allow_redirects=False,
                  headers={"Sec-Fetch-Mode": "navigate"})
        assert r.status_code == 302
        assert r.headers["Location"] == "/login.html"

    def test_anchored_cookie_not_shadowed(self, worker_session, base_url):
        session, _uid = worker_session
        real = session.cookies.get("access_token")
        s = requests.Session()
        s.cookies.set("xaccess_token", "FAKEFAKEFAKE")
        s.cookies.set("access_token", real)
        r = s.get(f"{base_url}/main.html", allow_redirects=False)
        assert r.status_code == 200
        assert "WORKER-HOME" in r.text

    def test_stale_cookie_navigation_to_login_page_not_looped(self, wait_for_gateway, base_url):
        # Регресс: правило 5 (st == "anon", просроченный/невалидный access_token)
        # раньше повторяло тот же баг, что и правило 2 — навигация на /login.html
        # с невалидной cookie редиректила саму на себя (TOO_MANY_REDIRECTS).
        s = requests.Session()
        s.cookies.set("access_token", "definitelynotarealtoken123456789")
        r = s.get(f"{base_url}/login.html", allow_redirects=False,
                  headers={"Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"})
        assert r.status_code == 200
        assert "LOGIN-PAGE" in r.text

    # ВНИМАНИЕ: держим flood-тест ПОСЛЕДНИМ в модуле — per-IP лимитер считает по
    # общему IP тест-контейнера, и на время окна (ROLE_ID_WINDOW_S) он «пачкает»
    # счётчик для следующих miss-path запросов с того же IP. Запуск последним не
    # даёт ему ронять соседние тесты (следующий файл стартует уже за окном).
    def test_distinct_token_flood_is_throttled(self, wait_for_gateway, base_url):
        # Per-worker ivm backstop срабатывает, когда flood уникальных токенов
        # концентрируется на ОДНОМ keep-alive соединении (→ один prefork-воркер).
        # Каждый уникальный токен — ivm-miss → инкремент per-IP счётчика ДО curl;
        # после ROLE_ID_RATE_LIMIT (деф. 60/10s) — 503 без похода в ACM.
        # (Размазанный по соединениям flood воркер поймать не может — для этого
        # на TLS-терминаторе стоит limit_req, это глобальный лимитер.)
        s = requests.Session()  # один Session → keep-alive → один воркер
        codes = []
        for i in range(200):
            s.cookies.set("access_token", f"floodtoken{i:08d}")
            codes.append(s.get(f"{base_url}/main.html", allow_redirects=False,
                               headers={"Sec-Fetch-Mode": "navigate"}).status_code)
        assert 503 in codes, "rate limiter did not engage under distinct-token flood"
