--[[
  jwt_auth.lua — аутентификация и авторизация через ACM для Apache mod_lua.

  Каждый защищённый запрос: читаем Cookie → извлекаем access_token →
  передаём в acm:8004/acm/check вместе с методом и путём → устанавливаем
  X-Remote-User, X-Remote-Role, X-Remote-Lockers, X-Remote-Require-2FA
  для бэкенд-сервисов.

  Все три значения (access_token, method, uri) экранируются через shell_quote
  перед интерполяцией в curl. POSIX-quote (`'` → `'\''`) защищает от инъекции.
  Метод дополнительно валидируется по whitelist.

  Публичные пути (без проверки): /api/v1/auth/*, /health
  ACM сам возвращает 200 для публичных путей без токена.
--]]
local common = require "scud_common"

-- Whitelist HTTP методов (defense-in-depth поверх Apache-валидации).
local ALLOWED_METHODS = {
    GET = true, POST = true, PUT = true, PATCH = true,
    DELETE = true, OPTIONS = true, HEAD = true,
}

function check_auth(r)
    -- 1. Блокируем подделку контекстных заголовков от клиента
    r.headers_in['X-Remote-User']         = nil
    r.headers_in['X-Remote-Role']         = nil
    r.headers_in['X-Remote-Lockers']      = nil
    r.headers_in['X-Remote-Require-2FA']  = nil
    -- Anti-spoof: клиентский X-Request-ID не доходит до сервисов как есть.
    r.headers_in['X-Request-ID']          = nil

    -- Минтим correlation-id для всего проксируемого трафика. Читаем
    -- /proc/sys/kernel/random/uuid (без subprocess), нормализуем в hex.
    local request_id
    local uuid_f = io.open("/proc/sys/kernel/random/uuid", "r")
    if uuid_f then
        request_id = (uuid_f:read("*l") or ""):gsub("-", "")
        uuid_f:close()
    end
    if not request_id or request_id == "" then
        request_id = string.format("%08x%08x", os.time(), math.random(0, 0xffffffff))
    end
    r.headers_in['X-Request-ID'] = request_id
    r.subprocess_env['REQUEST_ID'] = request_id

    local uri    = r.uri
    local method = r.method

    -- 2. Для всех /api/v1/* и /health — делегируем проверку ACM
    if uri:sub(1, 8) ~= "/api/v1/" and uri ~= "/health" then
        return apache2.OK
    end

    -- 2a. Проверяем метод по whitelist до вызова ACM (defense-in-depth).
    if not ALLOWED_METHODS[method] then
        return send_error(r, 400, "Invalid HTTP method")
    end

    -- 3. Извлекаем access_token из Cookie.
    --    token_urlsafe(32) содержит только A-Za-z0-9_- — regex дополнительно
    --    отсекает любые иные символы (всё равно shell-quoting закрывает все векторы).
    local access_token = common.extract_access_token(r.headers_in['Cookie']) or ""

    -- 4. Вызываем ACM — он сам разберёт, нужен ли токен для данного пути.
    --    Все три значения shell-quoted, чтобы исключить shell injection.
    --    --max-time 2: при деградации ACM Apache-воркер блокируется минимум
    --    на это время; короткий потолок ускоряет освобождение pool'а.
    local cmd = string.format(
        "curl -si --max-time 2 -H %s -H %s -H %s http://acm:8004/acm/check 2>/dev/null",
        common.shell_quote("X-Access-Token: " .. access_token),
        common.shell_quote("X-Request-Method: " .. method),
        common.shell_quote("X-Request-Path: " .. uri)
    )
    local pipe     = io.popen(cmd)
    local response = pipe:read("*a")
    pipe:close()

    local status = tonumber(response:match("HTTP/%d+%.?%d* (%d+)"))

    if not status then
        return send_error(r, 502, "ACM service unavailable")
    end

    if status == 401 then
        local detail = response:match('"detail":"([^"]+)"') or "Unauthorized"
        return send_error(r, 401, detail)
    end

    if status == 403 then
        local detail = response:match('"detail":"([^"]+)"') or "Forbidden"
        return send_error(r, 403, detail)
    end

    if status ~= 200 then
        return send_error(r, 502, "Unexpected ACM response: " .. tostring(status))
    end

    -- 5. Извлекаем контекстные заголовки из ответа ACM
    -- Значение читаем ДО конца строки: `%s*(%S+)` перешагнул бы через перевод
    -- строки и захватил имя СЛЕДУЮЩЕГО заголовка. Это не теория: у worker'а
    -- без Допусков ACM шлёт `X-Remote-Lockers:` с пустым значением, и старый
    -- паттерн отдавал в locker_api строку "X-Remote-Require-2FA:" вместо "".
    -- Пустое значение обязано оставаться пустым: на нём построена область
    -- видимости шкафов (см. shared/access_control/locker_grants.py).
    local user_id     = response:match("[Xx]%-[Uu]ser%-[Ii]d:[ \t]*([^\r\n]*)")
    local role        = response:match("[Xx]%-[Rr]emote%-[Rr]ole:[ \t]*([^\r\n]*)")
    local lockers     = response:match("[Xx]%-[Rr]emote%-[Ll]ockers:[ \t]*([^\r\n]*)")
    local require_2fa = response:match("[Xx]%-[Rr]emote%-[Rr]equire%-2[Ff][Aa]:[ \t]*([^\r\n]*)")

    if not user_id or user_id == "" then
        -- Публичный путь — ACM вернул 200 без заголовков.
        -- Cookie не трогаем: auth-эндпоинтам (logout) он нужен.
        return apache2.OK
    end

    -- 6. Проставляем заголовки, убираем cookie чтобы не пробрасывать в бэкенды
    r.headers_in['X-Remote-User']        = user_id
    r.headers_in['X-Remote-Role']        = role        or ""
    r.headers_in['X-Remote-Lockers']     = lockers     or ""
    r.headers_in['X-Remote-Require-2FA'] = require_2fa or "false"
    r.headers_in['Cookie']               = nil

    return apache2.OK
end

function send_error(r, code, detail)
    r.status       = code
    r.content_type = "application/problem+json; charset=utf-8"
    local title, type_uri
    if code == 400 then
        title    = "Bad Request"
        type_uri = "https://api.scuddocs.com/problems/bad-request"
    elseif code == 401 then
        title    = "Unauthorized"
        type_uri = "https://api.scuddocs.com/problems/unauthorized"
    elseif code == 403 then
        title    = "Forbidden"
        type_uri = "https://api.scuddocs.com/problems/forbidden"
    else
        title    = "Service Unavailable"
        type_uri = "https://api.scuddocs.com/problems/service-unavailable"
    end
    local rid = r.subprocess_env and r.subprocess_env['REQUEST_ID']
    if rid and rid ~= "" then
        r.headers_out['X-Request-ID'] = rid
        r.err_headers_out['X-Request-ID'] = rid
    end
    r:write(string.format(
        '{"type":"%s","title":"%s","status":%d,"detail":"%s"}',
        type_uri, title, code, detail
    ))
    return apache2.DONE
end
