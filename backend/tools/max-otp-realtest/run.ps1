<#
.SYNOPSIS
  Запуск полу-ручного end-to-end теста OTP через реальный Max-бот одной командой.

.DESCRIPTION
  Делает всё за один вызов:
    1. сеет dev-секреты (postgres_password и пр.), если их нет;
    2. кладёт реальный токен бота в backend/secrets/max_bot_token;
    3. поднимает backend-стек (docker compose up -d --build --wait) с этим токеном;
    4. запускает интерактивный харнесс (docker compose run max_otp_realtest);
    5. по выходу делает docker compose down -v (если не задан -Keep).

  Токен берётся из -Token, иначе из $env:MAX_BOT_TOKEN, иначе из уже лежащего
  непустого secrets/max_bot_token. Username бота — из -BotUsername или
  $env:MAX_BOT_USERNAME (нужен для сборки invite-ссылки).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File backend\tools\max-otp-realtest\run.ps1 -Token 'ТОКЕН' -BotUsername 'mybot'

.EXAMPLE
  # токен уже лежит в secrets/max_bot_token — достаточно username:
  powershell -ExecutionPolicy Bypass -File backend\tools\max-otp-realtest\run.ps1 -BotUsername 'mybot'
#>
[CmdletBinding()]
param(
    [string]$Token = $env:MAX_BOT_TOKEN,
    [string]$BotUsername = $env:MAX_BOT_USERNAME,
    # Не делать `docker compose down -v` после теста (оставить стек поднятым).
    [switch]$Keep
)

$ErrorActionPreference = 'Stop'

# backend/ — два уровня вверх от этого скрипта (backend/tools/max-otp-realtest/).
$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$backendDir = (Resolve-Path (Join-Path $scriptDir '..\..')).Path
$secretFile = Join-Path $backendDir 'secrets\max_bot_token'

# Токен: параметр/env, иначе переиспользуем уже лежащий непустой secret-файл.
if (-not $Token -and (Test-Path $secretFile)) {
    $existing = (Get-Content -Raw -Path $secretFile -ErrorAction SilentlyContinue)
    if ($existing) { $Token = $existing.Trim() }
}
if (-not $Token) {
    throw "Нет токена бота. Передай -Token '<токен из @MasterBot>' или задай `$env:MAX_BOT_TOKEN."
}
if (-not $BotUsername) {
    throw "Нет username бота. Передай -BotUsername '<имя>' или задай `$env:MAX_BOT_USERNAME (нужен для invite-ссылки)."
}

Push-Location $backendDir
try {
    Write-Host "==> dev-секреты" -ForegroundColor Cyan
    bash scripts/seed-dev-secrets.sh
    if ($LASTEXITCODE -ne 0) { throw "seed-dev-secrets.sh завершился с кодом $LASTEXITCODE" }

    Write-Host "==> токен бота -> secrets/max_bot_token" -ForegroundColor Cyan
    # ascii => без BOM (PowerShell 5.1 по умолчанию пишет UTF-16, что сломало бы read_secret).
    Set-Content -Path $secretFile -Value $Token -NoNewline -Encoding ascii

    # MAX_BOT_USERNAME подхватят и user_api (для invite-ссылки), и харнесс.
    $env:MAX_BOT_USERNAME = $BotUsername

    Write-Host "==> поднимаю стек (это пара минут на холодную)" -ForegroundColor Cyan
    docker compose up -d --build --wait
    if ($LASTEXITCODE -ne 0) { throw "docker compose up завершился с кодом $LASTEXITCODE" }

    Write-Host "==> запускаю тест (следуй подсказкам: открой ссылку в Max, введи код)" -ForegroundColor Cyan
    docker compose --profile manual run --rm --build max_otp_realtest
}
finally {
    if ($Keep) {
        Write-Host "==> -Keep: стек оставлен поднятым. Прибрать вручную: cd backend; docker compose down -v" -ForegroundColor Yellow
    } else {
        Write-Host "==> очистка: docker compose down -v" -ForegroundColor Cyan
        docker compose down -v
    }
    Pop-Location
}
