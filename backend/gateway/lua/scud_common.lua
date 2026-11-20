--[[
  scud_common.lua — общие помощники для Lua-хуков gateway (jwt_auth, static_route).
  Загружается через `LuaPackagePath /etc/apache2/lua/?.lua` + `require "scud_common"`.
--]]
local M = {}

-- POSIX-надёжный shell-quote: оборачивает в одинарные кавычки; внутри них shell
-- интерпретирует только саму ' — закрывает ', ;, `, $(...), \n, \, " .
function M.shell_quote(s)
    return "'" .. (s:gsub("'", "'\\''")) .. "'"
end

-- Извлекает access_token из Cookie, привязываясь к границе cookie, чтобы
-- похожее имя (xaccess_token=...) не подменяло настоящий cookie.
-- token_urlsafe(32): charset A-Za-z0-9_-.
function M.extract_access_token(cookie_header)
    local c = "; " .. (cookie_header or "")
    return c:match("[;%s]access_token=([A-Za-z0-9%-_]+)")
end

return M
