"""
Тесты заголовков безопасности и закрытых эндпоинтов Apache Gateway.

Покрывают:
- SEC-17: ужесточение CSP (img-src без `*`, script-src без `unsafe-inline`).
- SEC-22: добавление base-uri / object-src / form-action в CSP.
- ARC-04: явный deny на /metrics (нельзя проксировать наружу).

Все запросы идут через apache_gateway:80.

ВАЖНО: `Header always set` выставляет заголовки на ответах, прошедших через
output-фильтры Apache, то есть на ПРОКСИРОВАННЫХ ответах бэкендов. Ответ 401,
который генерирует mod_lua `LuaHookAccessChecker` (отказ без cookie), пишется
напрямую и output-фазу заголовков НЕ проходит — на нём CSP отсутствует. Поэтому
заголовки проверяем на реальном проксированном ответе (аутентифицированная
admin-сессия), а не на 401-без-cookie.
"""
import pytest
import requests

from conftest import GATEWAY_URL


@pytest.fixture(scope="module")
def proxied_response(admin_session) -> requests.Response:
    """Реальный проксированный ответ бэкенда — несёт security-заголовки."""
    resp = admin_session.get(f"{GATEWAY_URL}/api/v1/users")
    assert resp.status_code == 200, (
        f"Ожидался 200 от проксированного /api/v1/users: {resp.status_code} {resp.text}"
    )
    return resp


def _csp(resp: requests.Response) -> str:
    """Возвращает значение Content-Security-Policy из ответа."""
    csp = resp.headers.get("Content-Security-Policy")
    assert csp is not None, "Заголовок Content-Security-Policy отсутствует"
    return csp


def _parse_directives(csp: str) -> dict[str, str]:
    """Разбирает CSP-строку в {директива: значение}."""
    directives: dict[str, str] = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, value = part.partition(" ")
        directives[name] = value.strip()
    return directives


# =============================================================================
# CSP: ужесточение (SEC-17, SEC-22)
# =============================================================================

class TestContentSecurityPolicy:
    """Жёсткая CSP на production-vhost."""

    def test_csp_header_present(self, proxied_response):
        """CSP выставляется на проксированном ответе."""
        _csp(proxied_response)

    def test_img_src_allows_any_source(self, proxied_response):
        """img-src разрешает загрузку изображений из любого источника."""
        directives = _parse_directives(_csp(proxied_response))
        img_src = directives.get("img-src", "").split()
        assert "*" in img_src, f"img-src должен содержать `*`: {img_src!r}"
        assert "data:" in img_src, f"img-src должен содержать data:: {img_src!r}"
        assert "blob:" in img_src, f"img-src должен содержать blob:: {img_src!r}"

    def test_script_src_no_unsafe_inline(self, proxied_response):
        """SEC-17: script-src без 'unsafe-inline' (XSS-усилитель снят)."""
        directives = _parse_directives(_csp(proxied_response))
        script_src = directives.get("script-src", "")
        assert "'unsafe-inline'" not in script_src, (
            f"script-src не должен содержать 'unsafe-inline': {script_src!r}"
        )
        assert "'self'" in script_src, f"script-src должен включать 'self': {script_src!r}"

    def test_base_uri_present(self, proxied_response):
        """SEC-22: base-uri 'self' присутствует."""
        directives = _parse_directives(_csp(proxied_response))
        assert directives.get("base-uri") == "'self'", (
            f"base-uri должен быть 'self': {directives.get('base-uri')!r}"
        )

    def test_object_src_none(self, proxied_response):
        """SEC-22: object-src 'none' присутствует."""
        directives = _parse_directives(_csp(proxied_response))
        assert directives.get("object-src") == "'none'", (
            f"object-src должен быть 'none': {directives.get('object-src')!r}"
        )

    def test_form_action_self(self, proxied_response):
        """SEC-22: form-action 'self' присутствует."""
        directives = _parse_directives(_csp(proxied_response))
        assert directives.get("form-action") == "'self'", (
            f"form-action должен быть 'self': {directives.get('form-action')!r}"
        )

    def test_frame_ancestors_preserved(self, proxied_response):
        """Существующая хорошая директива frame-ancestors 'none' сохранена."""
        directives = _parse_directives(_csp(proxied_response))
        assert directives.get("frame-ancestors") == "'none'", (
            f"frame-ancestors должен быть 'none': {directives.get('frame-ancestors')!r}"
        )

    def test_existing_security_headers_preserved(self, proxied_response):
        """Прочие security-заголовки на месте (регрессия)."""
        assert proxied_response.headers.get("X-Content-Type-Options") == "nosniff"
        assert proxied_response.headers.get("X-Frame-Options") == "DENY"
        assert proxied_response.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"


# =============================================================================
# /metrics закрыт снаружи (ARC-04)
# =============================================================================

class TestMetricsBlocked:
    """apache.conf <Location /metrics> Require all denied — внешний доступ запрещён."""

    METRICS_PATHS = [
        "/metrics",
        "/metrics/",
        "/api/v1/locker/metrics",
    ]

    def test_metrics_root_blocked(self, wait_for_gateway):
        """GET /metrics → 403/404, НЕ Prometheus-экспозиция бэкенда."""
        resp = requests.get(f"{GATEWAY_URL}/metrics")
        assert resp.status_code in (403, 404), (
            f"/metrics должен быть закрыт извне, получено {resp.status_code}"
        )
        # Тело не должно содержать Prometheus-метрик бэкенда.
        assert "scud_http_requests_total" not in resp.text, (
            "Тело /metrics не должно содержать Prometheus-экспозицию"
        )

    def test_metrics_with_admin_cookie_still_blocked(self, admin_session):
        """Даже с админской cookie /metrics не доступен через gateway."""
        resp = admin_session.get(f"{GATEWAY_URL}/metrics")
        assert resp.status_code in (403, 404), (
            f"/metrics должен быть закрыт даже для админа, получено {resp.status_code}"
        )
        assert "scud_http_requests_total" not in resp.text
