--[[
  health.lua — gateway-уровневый health endpoint.

  Минимальный 200 OK с фиксированным телом: mod_status раскрывает версию
  Apache, worker-state и URL клиентов, поэтому использовать его на
  публичном /health нельзя.

  Подключается через `LuaMapHandler` в `<Location /health>` apache.conf'а.
--]]

function handle(r)
    r.status = 200
    r.content_type = "text/plain; charset=utf-8"
    r:write("ok\n")
    return apache2.OK
end
