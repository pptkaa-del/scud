--[[
  static_route.lua — LuaHookTranslateName: ролевая раздача статики.
  Публичное-vs-ролевое определяется по session-cookie, НЕ по пути.
  API (/api/v1 через /acm/check) остаётся реальной границей авторизации;
  ролевая раздача статики — defense-in-depth + корректный UX.
--]]
local common = require "scud_common"

local PUBLIC_DIR = "/var/www/public"
local ROLE_DIR = {
    worker        = "/var/www/roles/worker",
    operator      = "/var/www/roles/operator",
    administrator = "/var/www/roles/admin",
}

local function envnum(name, default)
    return tonumber(os.getenv(name) or "") or default
end

local function role_home(role)
    local v = os.getenv("ROLE_HOME_" .. role:upper())
    if v == nil or v == "" then v = os.getenv("ROLE_HOME_DEFAULT") end
    if v == nil or v == "" then v = "/main.html" end
    -- Домашняя страница не может быть "/" или "/login.html": иначе rule 3
    -- зациклит редирект (→ LimitInternalRecursion → 500). Подстраховка от
    -- ошибочного ROLE_HOME_* — откатываемся на безопасный дефолт.
    if v == "/" or v == "/login.html" then v = "/main.html" end
    return v
end

-- Reject traversal / encoded / control chars BEFORE any filename concat.
-- Frontend asset paths are plain ASCII и никогда не содержат %-кодирования,
-- поэтому любой % отвергается (убирает неоднозначность декодирования %2e).
local function unsafe_path(uri)
    return uri:find("%%")
        or uri:find("%.%.", 1, true)
        or uri:find("//", 1, true)
        or uri:find("\\", 1, true)
        or uri:find("[%z\1-\31]")
end

-- Navigation vs asset: Sec-Fetch-* (браузер ставит, страница подделать не может)
-- — основной сигнал; Accept + расширение — fallback для старых клиентов.
local function is_navigation(r)
    local mode = r.headers_in['Sec-Fetch-Mode']
    if mode and mode ~= "" then return mode == "navigate" end
    local dest = r.headers_in['Sec-Fetch-Dest']
    if dest and dest ~= "" then return dest == "document" end
    local uri = r.uri
    local accept = r.headers_in['Accept'] or ""
    local looks_page = (uri:match("%.html$") ~= nil) or (uri:match("%.[%a%d]+$") == nil)
    return looks_page and accept:find("text/html", 1, true) ~= nil
end

local function serve(r, base)
    r.filename = base .. r.uri   -- r.uri не трогаем → mod_mime видит реальное расширение
    return apache2.OK
end

local function redirect(r, loc)
    r.err_headers_out['Location'] = loc
    r.err_headers_out['Cache-Control'] = 'no-store'
    r.status = 302
    return apache2.DONE
end

-- Неаутентифицированная/невалидная сессия. Навигация браузера на маршрут приложения → /login.html;
-- сам /login.html (и любой публичный ассет) отдаём как есть. Исключение для /login.html ОБЯЗАТЕЛЬНО:
-- без него навигация (Sec-Fetch-Mode: navigate) на /login.html редиректится сама на себя → бесконечный
-- цикл (TOO_MANY_REDIRECTS).
local function serve_public_or_login(r)
    if r.uri ~= "/login.html" and is_navigation(r) then
        return redirect(r, "/login.html")
    end
    return serve(r, PUBLIC_DIR)
end

local function fail_closed(r)
    r.status = 503
    r.content_type = "application/problem+json; charset=utf-8"
    r:write('{"type":"https://api.scuddocs.com/problems/service-unavailable",'
        .. '"title":"Service Unavailable","status":503,'
        .. '"detail":"Identity service unavailable"}')
    return apache2.DONE
end

-- Per-IP fixed-window throttle на miss-path (per-worker backstop; глобальный
-- лимитер — limit_req на TLS-терминаторе). true = превышен бюджет.
local function over_rate_limit(r)
    local limit  = envnum("ROLE_ID_RATE_LIMIT", 60)
    local window = envnum("ROLE_ID_WINDOW_S", 10)
    local ip     = r.useragent_ip or "?"
    local now    = os.time()
    local key    = "rl:" .. ip
    local count, start = 0, now
    local raw = r:ivm_get(key)
    if raw then
        local c, s = raw:match("^(%d+)|(%d+)$")
        if c then count, start = tonumber(c), tonumber(s) end
    end
    if now - start >= window then count, start = 0, now end
    count = count + 1
    r:ivm_set(key, count .. "|" .. start)
    return count > limit
end

-- Role resolution, ivm-cached per worker, bucketed to bound memory (B2).
--   role, "ok"    — аутентифицирован
--   nil,  "anon"  — нет/невалиден/истёк → как неаутентифицированный (правило 5)
--   nil,  "fail"  — identity недоступен → fail closed (503)
local function resolve_role(r, token)
    local ttl      = envnum("ROLE_CACHE_TTL_S", 5)
    local fail_ttl = envnum("ROLE_FAIL_TTL_S", 1)
    local maxn     = envnum("ROLE_CACHE_MAX", 4096)
    local now      = os.time()
    local h        = r:sha1(token)
    local key      = "role:" .. (tonumber(h:sub(1, 8), 16) % maxn)

    local raw = r:ivm_get(key)
    if raw then
        local sh, val, exp = raw:match("^(%x+)|(.-)|(%d+)$")
        if sh == h and val and tonumber(exp) > now then
            if val == "-" then return nil, "anon" end
            return val, "ok"
        end
    end

    if over_rate_limit(r) then return nil, "fail" end

    local cmd = string.format(
        "curl -si --max-time 2 -H %s http://acm:8004/acm/identity 2>/dev/null",
        common.shell_quote("X-Access-Token: " .. token))
    local pipe = io.popen(cmd)
    if not pipe then return nil, "fail" end   -- fork под нагрузкой не удался → fail closed
    local resp = pipe:read("*a")
    pipe:close()
    local status = tonumber(resp:match("HTTP/%d+%.?%d* (%d+)"))

    local function store(val, life)
        r:ivm_set(key, h .. "|" .. val .. "|" .. (now + life))
    end

    if status == 200 then
        local role = resp:match("[Xx]%-[Rr]emote%-[Rr]ole:%s*(%a+)")
        role = role and role:lower() or nil
        if role and ROLE_DIR[role] then store(role, ttl); return role, "ok" end
        store("-", ttl); return nil, "anon"   -- unknown role: NEVER default admin
    elseif status == 401 then
        store("-", ttl); return nil, "anon"
    else
        store("-", fail_ttl); return nil, "fail"
    end
end

function static_route(r)
    local uri = r.uri

    -- 0. Служебные/проксируемые пути и OPTIONS — назад в core без изменений.
    if r.method == "OPTIONS"
        or uri:sub(1, 8) == "/api/v1/"
        or uri == "/health"
        or uri:sub(1, 8) == "/metrics"
        or uri:sub(1, 9) == "/internal"
        or uri:sub(1, 4) == "/acm" then
        return apache2.DECLINED
    end

    -- Traversal-фенс до любой конкатенации пути.
    if unsafe_path(uri) then
        r.status = 400
        return apache2.DONE
    end

    -- 1. Всегда-публичные, роль-независимые ассеты.
    if uri:sub(1, 6) == "/libs/" or uri == "/favicon.ico" then
        return serve(r, PUBLIC_DIR)
    end

    local token = common.extract_access_token(r.headers_in['Cookie'])

    -- 2. Неаутентифицированный: навигация → login; иначе публичный ассет (404 если нет).
    if not token then
        return serve_public_or_login(r)
    end

    -- Аутентифицирован: резолвим роль (ivm-cached, авторитетно через ACM).
    local role, st = resolve_role(r, token)
    if st == "fail" then return fail_closed(r) end
    if st == "anon" then    -- правило 5: истёк/невалиден → как правило 2
        return serve_public_or_login(r)
    end

    -- 3. Уже вошёл, но на "/" или "/login.html" → домашняя страница роли.
    if uri == "/" or uri == "/login.html" then
        return redirect(r, role_home(role))
    end

    -- 4. Раздаём только из монтирования этой роли (no-store вешает <Directory>).
    return serve(r, ROLE_DIR[role])
end
