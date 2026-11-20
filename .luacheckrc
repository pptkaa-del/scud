-- luacheck config для Lua-скриптов Apache mod_lua в backend/gateway/lua/.
-- Mod_lua экспонирует apache2.* и ngx.* API как глобалы.

std = "min+busted"

globals = {
    "ngx",
    "apache2",
    -- Apache mod_lua handler-функции регистрируются как глобалы (LuaMapHandler/
    -- LuaAuthzProvider обращается к ним по имени). Это легитимный паттерн
    -- mod_lua, не лимит globals.
    "check_auth",
    "handle",
    "send_error",
    "authz_provider",
    "static_route",
}

read_globals = {
    "string", "table", "os", "io", "math",
    "ipairs", "pairs", "type", "tostring", "tonumber",
    "pcall", "error", "assert", "print", "require",
}

max_line_length = 120
-- Cyclomatic complexity 25: jwt_auth.lua делает много веток (валидация
-- claims, expiry, role, headers). Разбивать на под-функции через mod_lua
-- неудобно (каждая должна быть глобалом), пока не блокируем.
max_cyclomatic_complexity = 25

exclude_files = {
    "emulator/**",
    "tests/**",
    ".git/**",
}

-- В Apache mod_lua handler-функция получает request объект как первый аргумент
-- но не всегда используется (например для статических endpoint).
-- ВАЖНО: luacheck `files[key]` берёт ключ как Lua-pattern, не glob — нужен
-- полный путь файла или regex-pattern, иначе ignore тихо не сработает.
files["backend/gateway/lua/jwt_auth.lua"] = {
    ignore = {"212"},   -- unused argument
}
files["backend/gateway/lua/health.lua"] = {
    ignore = {"212"},
}
