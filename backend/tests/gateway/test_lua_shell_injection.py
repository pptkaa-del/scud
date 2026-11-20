"""
Регрессионные тесты на shell injection в gateway/lua/jwt_auth.lua.

Lua-обработчик передаёт URI пользователя в curl через io.popen. Без
экранирования символы `'`, `;`, `` ` ``, `$()` могут привести к RCE в
gateway-процессе.

Стратегия проверки: отправляем неаутентифицированный запрос с URI,
содержащим вредоносные shell-метасимволы. Если экранирование работает —
запрос корректно доходит до ACM, который возвращает 401 (нет токена),
и Lua транслирует это в 401 для клиента.

Если экранирование сломано — curl получит мусорную команду, вернёт пустой
или невалидный ответ, `tonumber(response:match(...))` будет nil,
и Lua вернёт 502 "ACM service unavailable".

Поэтому ключевая ассерция в каждом тесте: status_code != 502.
"""
import urllib.parse

import requests

from conftest import GATEWAY_URL


def _raw_get(path: str) -> requests.Response:
    """
    GET через gateway с сохранением спец-символов в path.

    requests по умолчанию повторно квотит URL; передаём готовый PreparedRequest
    через urllib3, чтобы special chars действительно дошли до Apache в исходном
    виде (Apache затем декодирует их в r.uri для Lua).
    """
    session = requests.Session()
    req = requests.Request("GET", f"{GATEWAY_URL}{path}")
    prepared = session.prepare_request(req)
    # requests.PreparedRequest квотит URL через urllib — но % и спецсимволы
    # в path сохраняются как есть, если уже корректно проходят через urlsplit.
    return session.send(prepared, timeout=30)


def test_uri_with_single_quote_does_not_break_shell(wait_for_gateway):
    """
    URI с одинарной кавычкой не должен ломать shell-команду в Lua.
    Без экранирования: -H 'X-Request-Path: /api/v1/locker/x'y/sensors' →
    `y/sensors` интерпретируется shell-ом как отдельный аргумент,
    curl возвращает мусор, Lua возвращает 502.

    Главная ассерция: status_code != 502 (curl-команда не сломалась).
    Конкретный код (401/403/404) зависит от того, как ACM матчит путь
    с нестандартными символами; нас интересует только отсутствие server-side сбоя.
    """
    encoded = urllib.parse.quote("x'y", safe="")
    resp = requests.get(f"{GATEWAY_URL}/api/v1/locker/{encoded}/sensors", timeout=30)
    assert resp.status_code != 502, (
        f"Got 502 — shell command broke, Lua escape failed. Body: {resp.text}"
    )
    assert resp.status_code < 500, (
        f"Got 5xx — gateway stack failed. Status {resp.status_code}: {resp.text}"
    )


def test_uri_with_dollar_paren_does_not_break_shell(wait_for_gateway):
    """
    URI с $(...) не должен подставляться shell-ом.
    Без экранирования: $(id) выполнится как command substitution внутри curl-команды.

    Не используем `/` внутри payload (даже %-encoded `%2F`): Apache по умолчанию
    `AllowEncodedSlashes Off` отбрасывает такие запросы 404 ещё до Lua.
    """
    encoded = urllib.parse.quote("$(id)", safe="")
    resp = requests.get(f"{GATEWAY_URL}/api/v1/locker/{encoded}/sensors", timeout=30)
    assert resp.status_code != 502, (
        f"Got 502 — shell command broke, Lua escape failed. Body: {resp.text}"
    )
    assert resp.status_code < 500, (
        f"Got 5xx — gateway stack failed. Status {resp.status_code}: {resp.text}"
    )


def test_uri_with_backtick_does_not_break_shell(wait_for_gateway):
    """
    URI с backticks не должен подставляться shell-ом.
    Без экранирования: `id` выполнится как command substitution
    даже внутри двойных кавычек, и поломает curl-команду в одинарных.
    """
    encoded = urllib.parse.quote("`id`", safe="")
    resp = requests.get(f"{GATEWAY_URL}/api/v1/locker/{encoded}/sensors", timeout=30)
    assert resp.status_code != 502, (
        f"Got 502 — shell command broke, Lua escape failed. Body: {resp.text}"
    )
    assert resp.status_code < 500, (
        f"Got 5xx — gateway stack failed. Status {resp.status_code}: {resp.text}"
    )


def test_unknown_http_method_rejected_400(wait_for_gateway):
    """
    Неизвестный HTTP-метод должен быть отвергнут Lua до вызова ACM.
    Whitelist: GET, POST, PUT, PATCH, DELETE, OPTIONS, HEAD.
    """
    resp = requests.request(
        "BREW",
        f"{GATEWAY_URL}/api/v1/users",
        timeout=30,
    )
    assert resp.status_code != 502, (
        f"Got 502 — Lua should have rejected unknown method before calling ACM. "
        f"Body: {resp.text}"
    )
    assert resp.status_code == 400, (
        f"Expected 400 (Lua method whitelist rejection), "
        f"got {resp.status_code}: {resp.text}"
    )
    # Проверяем problem+json тело с ожидаемым detail.
    assert "Invalid HTTP method" in resp.text, (
        f"Expected 'Invalid HTTP method' in body, got: {resp.text}"
    )
