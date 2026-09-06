@echo off
setlocal EnableExtensions

set "COMPOSE_DIR=E:\dev\ai_agents\hermes\compose"
set "BACKUP_DIR=E:\dev\ai_agents\hermes\backups\kanban-db"
set "SERVICE=hermes"
set "CONTAINER=hermes"
set "DB=/var/lib/hermes-kanban/kanban.db"
set "BACKUP_PY=ZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoKaW1wb3J0IGhhc2hsaWIKaW1wb3J0IG9zCmltcG9ydCBzcWxpdGUzCmltcG9ydCBzeXMKZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCgpzb3VyY2UgPSBQYXRoKHN5cy5hcmd2WzFdKQpkZXN0aW5hdGlvbiA9IFBhdGgoc3lzLmFyZ3ZbMl0pCgppZiBkZXN0aW5hdGlvbi5leGlzdHMoKToKICAgIGRlc3RpbmF0aW9uLnVubGluaygpCgpzcmMgPSBzcWxpdGUzLmNvbm5lY3QoCiAgICBmImZpbGU6e3NvdXJjZX0/bW9kZT1ybyIsCiAgICB1cmk9VHJ1ZSwKICAgIHRpbWVvdXQ9MzAsCikKc3JjLmV4ZWN1dGUoIlBSQUdNQSBxdWVyeV9vbmx5ID0gT04iKQpzcmMuZXhlY3V0ZSgiUFJBR01BIGJ1c3lfdGltZW91dCA9IDMwMDAwIikKCmRzdCA9IHNxbGl0ZTMuY29ubmVjdChkZXN0aW5hdGlvbiwgdGltZW91dD0zMCkKCnRyeToKICAgIHNyYy5iYWNrdXAoZHN0LCBwYWdlcz0yNTYsIHNsZWVwPTAuMDUpCiAgICBkc3QuY29tbWl0KCkKCiAgICBpbnRlZ3JpdHkgPSBkc3QuZXhlY3V0ZSgiUFJBR01BIGludGVncml0eV9jaGVjayIpLmZldGNoYWxsKCkKICAgIHF1aWNrID0gZHN0LmV4ZWN1dGUoIlBSQUdNQSBxdWlja19jaGVjayIpLmZldGNoYWxsKCkKICAgIHRhc2tzID0gZHN0LmV4ZWN1dGUoIlNFTEVDVCBjb3VudCgqKSBGUk9NIHRhc2tzIikuZmV0Y2hvbmUoKQoKICAgIGlmIGludGVncml0eSAhPSBbKCJvayIsKV06CiAgICAgICAgcmFpc2UgUnVudGltZUVycm9yKGYiaW50ZWdyaXR5X2NoZWNrIGZhaWxlZDoge2ludGVncml0eX0iKQogICAgaWYgcXVpY2sgIT0gWygib2siLCldOgogICAgICAgIHJhaXNlIFJ1bnRpbWVFcnJvcihmInF1aWNrX2NoZWNrIGZhaWxlZDoge3F1aWNrfSIpCgpmaW5hbGx5OgogICAgZHN0LmNsb3NlKCkKICAgIHNyYy5jbG9zZSgpCgp3aXRoIGRlc3RpbmF0aW9uLm9wZW4oInJiKyIpIGFzIGhhbmRsZToKICAgIGhhbmRsZS5mbHVzaCgpCiAgICBvcy5mc3luYyhoYW5kbGUuZmlsZW5vKCkpCgpkaXJlY3RvcnlfZmQgPSBvcy5vcGVuKGRlc3RpbmF0aW9uLnBhcmVudCwgb3MuT19SRE9OTFkpCnRyeToKICAgIG9zLmZzeW5jKGRpcmVjdG9yeV9mZCkKZmluYWxseToKICAgIG9zLmNsb3NlKGRpcmVjdG9yeV9mZCkKCmRpZ2VzdCA9IGhhc2hsaWIuc2hhMjU2KGRlc3RpbmF0aW9uLnJlYWRfYnl0ZXMoKSkuaGV4ZGlnZXN0KCkKb3MuY2htb2QoZGVzdGluYXRpb24sIDBvNjAwKQoKcHJpbnQoImludGVncml0eT1vayIpCnByaW50KCJxdWlja19jaGVjaz1vayIpCnByaW50KGYidGFza3M9e3Rhc2tzWzBdfSIpCnByaW50KGYic2hhMjU2PXtkaWdlc3R9IikKcHJpbnQoZiJzaXplPXtkZXN0aW5hdGlvbi5zdGF0KCkuc3Rfc2l6ZX0iKQo="

if not exist "%COMPOSE_DIR%\docker-compose.yml" (
    echo [ERRO] docker-compose.yml nao encontrado em "%COMPOSE_DIR%".
    exit /b 1
)

if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"
if errorlevel 1 (
    echo [ERRO] Nao foi possivel criar "%BACKUP_DIR%".
    exit /b 1
)

cd /d "%COMPOSE_DIR%" || exit /b 1

set "RUNNING="
for /f "usebackq delims=" %%I in (`docker inspect -f "{{.State.Running}}" "%CONTAINER%" 2^>nul`) do set "RUNNING=%%I"
if /I not "%RUNNING%"=="true" (
    echo [ERRO] O container "%CONTAINER%" precisa estar em execucao para o backup online.
    exit /b 1
)

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"`) do set "STAMP=%%I"

set "NAME=kanban-%STAMP%.db"
set "FINAL=%BACKUP_DIR%\%NAME%"
set "PARTIAL=%FINAL%.partial"
set "SIDECAR=%FINAL%.sha256"
set "RESULT=%TEMP%\hermes-kanban-backup-%RANDOM%-%RANDOM%.txt"
set "CONTAINER_TMP=/tmp/%NAME%"

del /q "%PARTIAL%" "%RESULT%" 2>nul

echo [INFO] Criando snapshot SQLite consistente...
docker compose exec -T -u 10000:10000 "%SERVICE%" sh -c "printf '%%s' '%BACKUP_PY%' | base64 -d | python3 - '%DB%' '%CONTAINER_TMP%'" >"%RESULT%" 2>&1
if errorlevel 1 goto :backup_failed

type "%RESULT%"

echo [INFO] Copiando snapshot validado para o Windows...
docker compose cp "%CONTAINER%:%CONTAINER_TMP%" "%PARTIAL%" >nul
if errorlevel 1 goto :backup_failed

set "CONTAINER_HASH="
for /f "tokens=1" %%H in ('docker compose exec -T -u 10000:10000 "%SERVICE%" sha256sum "%CONTAINER_TMP%"') do set "CONTAINER_HASH=%%H"

set "HOST_HASH="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath '%PARTIAL%').Hash.ToLowerInvariant()"`) do set "HOST_HASH=%%H"

if not defined CONTAINER_HASH (
    echo [ERRO] Nao foi possivel calcular o hash no container.
    goto :backup_failed
)
if not defined HOST_HASH (
    echo [ERRO] Nao foi possivel calcular o hash no Windows.
    goto :backup_failed
)
if /I not "%CONTAINER_HASH%"=="%HOST_HASH%" (
    echo [ERRO] Hash divergente apos a copia.
    echo Container: %CONTAINER_HASH%
    echo Windows:   %HOST_HASH%
    goto :backup_failed
)

move /y "%PARTIAL%" "%FINAL%" >nul
if errorlevel 1 goto :backup_failed

>"%SIDECAR%" echo %HOST_HASH% *%NAME%
>"%BACKUP_DIR%\LATEST.txt" echo %NAME%

docker compose exec -T -u 10000:10000 "%SERVICE%" rm -f "%CONTAINER_TMP%" >nul 2>&1
del /q "%RESULT%" 2>nul

echo.
echo [OK] Backup concluido e verificado:
echo %FINAL%
echo SHA256: %HOST_HASH%
exit /b 0

:backup_failed
echo.
echo [ERRO] O backup nao foi concluido.
if exist "%RESULT%" type "%RESULT%"
del /q "%PARTIAL%" 2>nul
docker compose exec -T -u 10000:10000 "%SERVICE%" rm -f "%CONTAINER_TMP%" >nul 2>&1
del /q "%RESULT%" 2>nul
exit /b 1
