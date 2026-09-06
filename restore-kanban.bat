@echo off
setlocal EnableExtensions EnableDelayedExpansion

set "COMPOSE_DIR=E:\dev\ai_agents\hermes\compose"
set "BACKUP_DIR=E:\dev\ai_agents\hermes\backups\kanban-db"
set "SERVICE=hermes"
set "CONTAINER=hermes"
set "KANBAN_VOLUME=compose_hermes-kanban"
set "DB=/var/lib/hermes-kanban/kanban.db"
set "LOCK_DIR=%BACKUP_DIR%\.restore.lock"
set "VALIDATE_PY=ZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoKaW1wb3J0IGhhc2hsaWIKaW1wb3J0IG9zCmltcG9ydCBzcWxpdGUzCmltcG9ydCBzdGF0CmltcG9ydCBzeXMKZnJvbSBwYXRobGliIGltcG9ydCBQYXRoCgpwYXRoID0gUGF0aChzeXMuYXJndlsxXSkKY29ubiA9IHNxbGl0ZTMuY29ubmVjdCgKICAgIGYiZmlsZTp7cGF0aH0/bW9kZT1ybyIsCiAgICB1cmk9VHJ1ZSwKICAgIHRpbWVvdXQ9MzAsCikKdHJ5OgogICAgY29ubi5leGVjdXRlKCJQUkFHTUEgcXVlcnlfb25seSA9IE9OIikKICAgIGNvbm4uZXhlY3V0ZSgiUFJBR01BIGJ1c3lfdGltZW91dCA9IDMwMDAwIikKICAgIGludGVncml0eSA9IGNvbm4uZXhlY3V0ZSgiUFJBR01BIGludGVncml0eV9jaGVjayIpLmZldGNoYWxsKCkKICAgIHF1aWNrID0gY29ubi5leGVjdXRlKCJQUkFHTUEgcXVpY2tfY2hlY2siKS5mZXRjaGFsbCgpCiAgICB0YXNrcyA9IGNvbm4uZXhlY3V0ZSgiU0VMRUNUIGNvdW50KCopIEZST00gdGFza3MiKS5mZXRjaG9uZSgpCiAgICBqb3VybmFsID0gY29ubi5leGVjdXRlKCJQUkFHTUEgam91cm5hbF9tb2RlIikuZmV0Y2hvbmUoKQpmaW5hbGx5OgogICAgY29ubi5jbG9zZSgpCgppZiBpbnRlZ3JpdHkgIT0gWygib2siLCldOgogICAgcmFpc2UgU3lzdGVtRXhpdChmImludGVncml0eV9jaGVjayBmYWlsZWQ6IHtpbnRlZ3JpdHl9IikKaWYgcXVpY2sgIT0gWygib2siLCldOgogICAgcmFpc2UgU3lzdGVtRXhpdChmInF1aWNrX2NoZWNrIGZhaWxlZDoge3F1aWNrfSIpCmlmIG5vdCB0YXNrczoKICAgIHJhaXNlIFN5c3RlbUV4aXQoInRhc2tzIGNvdW50IHVuYXZhaWxhYmxlIikKaWYgbm90IGpvdXJuYWw6CiAgICByYWlzZSBTeXN0ZW1FeGl0KCJqb3VybmFsIG1vZGUgdW5hdmFpbGFibGUiKQoKZmlsZV9zdGF0ID0gcGF0aC5zdGF0KCkKZGlnZXN0ID0gaGFzaGxpYi5zaGEyNTYocGF0aC5yZWFkX2J5dGVzKCkpLmhleGRpZ2VzdCgpCm1vZGUgPSBzdGF0LlNfSU1PREUoZmlsZV9zdGF0LnN0X21vZGUpCgpwcmludCgiaW50ZWdyaXR5PW9rIikKcHJpbnQoInF1aWNrX2NoZWNrPW9rIikKcHJpbnQoZiJ0YXNrcz17dGFza3NbMF19IikKcHJpbnQoZiJqb3VybmFsPXtqb3VybmFsWzBdfSIpCnByaW50KGYic2hhMjU2PXtkaWdlc3R9IikKcHJpbnQoZiJvd25lcj17ZmlsZV9zdGF0LnN0X3VpZH06e2ZpbGVfc3RhdC5zdF9naWR9IikKcHJpbnQoZiJtb2RlPXttb2RlOm99IikK"
set "RESTORE_PY=ZnJvbSBfX2Z1dHVyZV9fIGltcG9ydCBhbm5vdGF0aW9ucwoKaW1wb3J0IG9zCmltcG9ydCBzaHV0aWwKaW1wb3J0IHNxbGl0ZTMKaW1wb3J0IHN5cwpmcm9tIHBhdGhsaWIgaW1wb3J0IFBhdGgKCnNvdXJjZSA9IFBhdGgoc3lzLmFyZ3ZbMV0pCnRhcmdldCA9IFBhdGgoc3lzLmFyZ3ZbMl0pCnRlbXBvcmFyeSA9IHRhcmdldC53aXRoX25hbWUodGFyZ2V0Lm5hbWUgKyAiLnJlc3RvcmUudG1wIikKCgpkZWYgdmFsaWRhdGUocGF0aDogUGF0aCkgLT4gdHVwbGVbaW50LCBzdHJdOgogICAgY29ubiA9IHNxbGl0ZTMuY29ubmVjdCgKICAgICAgICBmImZpbGU6e3BhdGh9P21vZGU9cm8iLAogICAgICAgIHVyaT1UcnVlLAogICAgICAgIHRpbWVvdXQ9MzAsCiAgICApCiAgICB0cnk6CiAgICAgICAgY29ubi5leGVjdXRlKCJQUkFHTUEgcXVlcnlfb25seSA9IE9OIikKICAgICAgICBjb25uLmV4ZWN1dGUoIlBSQUdNQSBidXN5X3RpbWVvdXQgPSAzMDAwMCIpCiAgICAgICAgaW50ZWdyaXR5ID0gY29ubi5leGVjdXRlKCJQUkFHTUEgaW50ZWdyaXR5X2NoZWNrIikuZmV0Y2hhbGwoKQogICAgICAgIHF1aWNrID0gY29ubi5leGVjdXRlKCJQUkFHTUEgcXVpY2tfY2hlY2siKS5mZXRjaGFsbCgpCiAgICAgICAgdGFza3MgPSBjb25uLmV4ZWN1dGUoIlNFTEVDVCBjb3VudCgqKSBGUk9NIHRhc2tzIikuZmV0Y2hvbmUoKQogICAgICAgIGpvdXJuYWwgPSBjb25uLmV4ZWN1dGUoIlBSQUdNQSBqb3VybmFsX21vZGUiKS5mZXRjaG9uZSgpCiAgICBmaW5hbGx5OgogICAgICAgIGNvbm4uY2xvc2UoKQoKICAgIGlmIGludGVncml0eSAhPSBbKCJvayIsKV06CiAgICAgICAgcmFpc2UgUnVudGltZUVycm9yKGYiaW50ZWdyaXR5X2NoZWNrIGZhaWxlZDoge2ludGVncml0eX0iKQogICAgaWYgcXVpY2sgIT0gWygib2siLCldOgogICAgICAgIHJhaXNlIFJ1bnRpbWVFcnJvcihmInF1aWNrX2NoZWNrIGZhaWxlZDoge3F1aWNrfSIpCiAgICBpZiBub3QgdGFza3M6CiAgICAgICAgcmFpc2UgUnVudGltZUVycm9yKCJ0YXNrcyBjb3VudCB1bmF2YWlsYWJsZSIpCiAgICBpZiBub3Qgam91cm5hbDoKICAgICAgICByYWlzZSBSdW50aW1lRXJyb3IoImpvdXJuYWwgbW9kZSB1bmF2YWlsYWJsZSIpCgogICAgcmV0dXJuIHRhc2tzWzBdLCBqb3VybmFsWzBdCgoKc291cmNlX3Rhc2tzLCBzb3VyY2Vfam91cm5hbCA9IHZhbGlkYXRlKHNvdXJjZSkKCmlmIHRlbXBvcmFyeS5leGlzdHMoKToKICAgIHRlbXBvcmFyeS51bmxpbmsoKQoKd2l0aCBzb3VyY2Uub3BlbigicmIiKSBhcyBzcmMsIHRlbXBvcmFyeS5vcGVuKCJ4YiIpIGFzIGRzdDoKICAgIHNodXRpbC5jb3B5ZmlsZW9iaihzcmMsIGRzdCwgbGVuZ3RoPTEwMjQgKiAxMDI0KQogICAgZHN0LmZsdXNoKCkKICAgIG9zLmZzeW5jKGRzdC5maWxlbm8oKSkKCm9zLmNobW9kKHRlbXBvcmFyeSwgMG82NjApCm9zLmNob3duKHRlbXBvcmFyeSwgMTAwMDAsIDEwMDAwKQoKdGVtcG9yYXJ5X3Rhc2tzLCB0ZW1wb3Jhcnlfam91cm5hbCA9IHZhbGlkYXRlKHRlbXBvcmFyeSkKaWYgdGVtcG9yYXJ5X3Rhc2tzICE9IHNvdXJjZV90YXNrczoKICAgIHJhaXNlIFJ1bnRpbWVFcnJvcigKICAgICAgICBmInRlbXBvcmFyeSB0YXNrIGNvdW50IGRpZmZlcnM6IHNvdXJjZT17c291cmNlX3Rhc2tzfSwgdGVtcG9yYXJ5PXt0ZW1wb3JhcnlfdGFza3N9IgogICAgKQppZiB0ZW1wb3Jhcnlfam91cm5hbCAhPSBzb3VyY2Vfam91cm5hbDoKICAgIHJhaXNlIFJ1bnRpbWVFcnJvcigKICAgICAgICBmInRlbXBvcmFyeSBqb3VybmFsIGRpZmZlcnM6IHNvdXJjZT17c291cmNlX2pvdXJuYWx9LCB0ZW1wb3Jhcnk9e3RlbXBvcmFyeV9qb3VybmFsfSIKICAgICkKCiMgQ3V0b3ZlciBiZWdpbnMgaGVyZS4gQW55IGZhaWx1cmUgYWZ0ZXIgdGhpcyBwb2ludCBtdXN0IHRyaWdnZXIgcm9sbGJhY2suCmZvciBzdWZmaXggaW4gKCItd2FsIiwgIi1zaG0iLCAiLWpvdXJuYWwiKToKICAgIGNhbmRpZGF0ZSA9IHRhcmdldC53aXRoX25hbWUodGFyZ2V0Lm5hbWUgKyBzdWZmaXgpCiAgICBpZiBjYW5kaWRhdGUuZXhpc3RzKCk6CiAgICAgICAgY2FuZGlkYXRlLnVubGluaygpCgpvcy5yZXBsYWNlKHRlbXBvcmFyeSwgdGFyZ2V0KQoKZGlyZWN0b3J5X2ZkID0gb3Mub3Blbih0YXJnZXQucGFyZW50LCBvcy5PX1JET05MWSkKdHJ5OgogICAgb3MuZnN5bmMoZGlyZWN0b3J5X2ZkKQpmaW5hbGx5OgogICAgb3MuY2xvc2UoZGlyZWN0b3J5X2ZkKQoKdGFza3MsIGpvdXJuYWwgPSB2YWxpZGF0ZSh0YXJnZXQpCgppZiBzb3VyY2UuZXhpc3RzKCk6CiAgICBzb3VyY2UudW5saW5rKCkKCnByaW50KCJyZXN0b3JlPW9rIikKcHJpbnQoImludGVncml0eT1vayIpCnByaW50KCJxdWlja19jaGVjaz1vayIpCnByaW50KGYidGFza3M9e3Rhc2tzfSIpCnByaW50KGYiam91cm5hbD17am91cm5hbH0iKQo="

set "EXIT_CODE=1"
set "LOCK_HELD="
set "SOURCE="
set "ROLLBACK_SOURCE="
set "LATEST_FILE=%BACKUP_DIR%\LATEST.txt"
set "LATEST_PRESERVE="
set "LATEST_ORIGINALLY_PRESENT="
set "VALIDATION=%TEMP%\hermes-kanban-restore-validation-%RANDOM%-%RANDOM%.txt"
set "INSPECT_JSON=%TEMP%\hermes-kanban-inspect-%RANDOM%-%RANDOM%.json"

if not exist "%COMPOSE_DIR%\docker-compose.yml" (
    echo [ERRO] docker-compose.yml nao encontrado em "%COMPOSE_DIR%".
    goto :finish
)
if not exist "%BACKUP_DIR%" (
    echo [ERRO] Diretorio de backups nao encontrado: "%BACKUP_DIR%".
    goto :finish
)

2>nul mkdir "%LOCK_DIR%"
if errorlevel 1 (
    echo [ERRO] Ja existe uma restauracao em andamento ou um lock residual:
    echo %LOCK_DIR%
    echo Confirme que nao ha outro restore ativo antes de remover esse diretorio.
    goto :finish
)
set "LOCK_HELD=1"

set "BACKUP_FILE="
set "BACKUP_SOURCE="

if "%~1"=="" (
    set "LATEST_FILE=%BACKUP_DIR%\LATEST.txt"
    if not exist "!LATEST_FILE!" (
        echo [ERRO] LATEST.txt nao encontrado: "!LATEST_FILE!".
        goto :finish
    )

    set "LATEST_NAME="
    set /p "LATEST_NAME="<"!LATEST_FILE!"
    if not defined LATEST_NAME (
        echo [ERRO] LATEST.txt esta vazio ou nao contem um nome de backup valido.
        goto :finish
    )

    for %%F in ("%BACKUP_DIR%\!LATEST_NAME!") do set "BACKUP_FILE=%%~fF"
    set "BACKUP_SOURCE=LATEST.txt"

    for %%F in ("!BACKUP_FILE!") do set "RESOLVED_BACKUP_DIR=%%~dpF"
    if /I not "!RESOLVED_BACKUP_DIR!"=="%BACKUP_DIR%\" (
        echo [ERRO] LATEST.txt deve apontar para um arquivo dentro de "%BACKUP_DIR%".
        goto :finish
    )
) else (
    if exist "%~1" (
        set "BACKUP_FILE=%~f1"
    ) else if exist "%BACKUP_DIR%\%~1" (
        for %%F in ("%BACKUP_DIR%\%~1") do set "BACKUP_FILE=%%~fF"
    ) else (
        set "BACKUP_FILE=%~1"
    )
    set "BACKUP_SOURCE=parametro"
)

if not defined BACKUP_FILE (
    echo [ERRO] Nenhum arquivo de backup foi selecionado.
    goto :finish
)
if not exist "!BACKUP_FILE!" (
    echo [ERRO] Backup nao encontrado: "!BACKUP_FILE!".
    goto :finish
)

for %%F in ("!BACKUP_FILE!") do (
    set "BACKUP_FILE=%%~fF"
    set "BACKUP_NAME=%%~nxF"
    set "BACKUP_EXT=%%~xF"
)
if /I not "!BACKUP_EXT!"==".db" (
    echo [ERRO] O arquivo precisa ter extensao .db.
    goto :finish
)

cd /d "%COMPOSE_DIR%" || goto :finish

set "RUNNING="
for /f "usebackq delims=" %%I in (`docker inspect -f "{{.State.Running}}" "%CONTAINER%" 2^>nul`) do set "RUNNING=%%I"
if /I not "!RUNNING!"=="true" (
    echo [ERRO] Inicie o container Hermes antes de executar a restauracao.
    goto :finish
)

docker inspect "%CONTAINER%" >"!INSPECT_JSON!" 2>nul
if errorlevel 1 (
    echo [ERRO] Nao foi possivel inspecionar o container Hermes.
    goto :finish
)

set "MOUNT_INFO="
for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "$i = ConvertFrom-Json -InputObject (Get-Content -Raw -LiteralPath '!INSPECT_JSON!'); $m = @($i[0].Mounts).Where({$_.Destination -eq '/var/lib/hermes-kanban'})[0]; if($null -ne $m){$m.Type + ':' + $m.Name}"`) do set "MOUNT_INFO=%%I"
del /q "!INSPECT_JSON!" 2>nul
if /I not "!MOUNT_INFO!"=="volume:%KANBAN_VOLUME%" (
    echo [ERRO] Mount do Kanban inesperado.
    echo Esperado: volume:%KANBAN_VOLUME%
    echo Atual:    !MOUNT_INFO!
    goto :finish
)

set "ACTUAL_HASH="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath '%BACKUP_FILE%').Hash.ToLowerInvariant()"`) do set "ACTUAL_HASH=%%H"
if not defined ACTUAL_HASH (
    echo [ERRO] Nao foi possivel calcular o SHA256 do backup.
    goto :finish
)

powershell -NoProfile -Command "if ('!ACTUAL_HASH!' -notmatch '^[0-9a-f]{64}$') { exit 1 }"
if errorlevel 1 (
    echo [ERRO] SHA256 calculado em formato invalido.
    goto :finish
)

if not exist "%BACKUP_FILE%.sha256" (
    echo [ERRO] Sidecar SHA256 ausente: "%BACKUP_FILE%.sha256"
    goto :finish
)

set "EXPECTED_HASH="
for /f "usebackq tokens=1" %%H in ("%BACKUP_FILE%.sha256") do set "EXPECTED_HASH=%%H"
if /I not "!EXPECTED_HASH!"=="!ACTUAL_HASH!" (
    echo [ERRO] O hash do arquivo nao corresponde ao sidecar SHA256.
    echo Esperado: !EXPECTED_HASH!
    echo Atual:    !ACTUAL_HASH!
    goto :finish
)

echo.
echo Backup selecionado ^(!BACKUP_SOURCE!^):
echo !BACKUP_FILE!
echo SHA256: !ACTUAL_HASH!
echo.
choice /C SN /N /M "Restaurar este backup? [S/N] "
if errorlevel 2 (
    set "EXIT_CODE=2"
    goto :finish
)

for /f "usebackq delims=" %%I in (`powershell -NoProfile -Command "Get-Date -Format yyyyMMdd-HHmmss"`) do set "STAMP=%%I"
set "SOURCE=/var/lib/hermes-kanban/.restore-source-!STAMP!-%RANDOM%.db"
set "ROLLBACK_SOURCE=/var/lib/hermes-kanban/.rollback-source-!STAMP!-%RANDOM%.db"

echo.
echo [INFO] Copiando e validando o backup selecionado dentro do volume Linux...
docker compose cp "%BACKUP_FILE%" "%CONTAINER%:!SOURCE!" >nul
if errorlevel 1 goto :failed_while_running

docker compose exec -T -u 0:0 "%SERVICE%" sh -c "chown 10000:10000 '!SOURCE!' && chmod 600 '!SOURCE!'" >nul
if errorlevel 1 goto :failed_while_running

docker compose exec -T -u 10000:10000 "%SERVICE%" sh -c "printf '%%s' '%VALIDATE_PY%' | base64 -d | python3 - '!SOURCE!'" >"!VALIDATION!" 2>&1
if errorlevel 1 goto :failed_while_running
type "!VALIDATION!"

set "VOLUME_HASH="
for /f "usebackq tokens=1,* delims==" %%A in ("!VALIDATION!") do (
    if /I "%%A"=="sha256" set "VOLUME_HASH=%%B"
)
if not defined VOLUME_HASH (
    echo [ERRO] A validacao do arquivo no volume nao retornou SHA256.
    goto :failed_while_running
)
if /I not "!VOLUME_HASH!"=="!ACTUAL_HASH!" (
    echo [ERRO] O arquivo copiado para o volume nao corresponde ao hash validado no Windows.
    echo Hash Windows: !ACTUAL_HASH!
    echo Hash volume:  !VOLUME_HASH!
    goto :failed_while_running
)

echo [INFO] Criando backup de seguranca do banco atual sem alterar LATEST.txt...
set "LATEST_PRESERVE=%BACKUP_DIR%\.LATEST.restore-preserve-!STAMP!-%RANDOM%.tmp"
set "LATEST_ORIGINALLY_PRESENT=0"

if exist "!LATEST_FILE!" (
    copy /b /y "!LATEST_FILE!" "!LATEST_PRESERVE!" >nul
    if errorlevel 1 (
        echo [ERRO] Nao foi possivel preservar LATEST.txt antes do backup pre-restauracao.
        goto :failed_while_running
    )
    set "LATEST_ORIGINALLY_PRESENT=1"
)

call "%~dp0backup-kanban.bat"
set "PRE_BACKUP_EXIT=!ERRORLEVEL!"

set "PRE_BACKUP_NAME="
if exist "!LATEST_FILE!" set /p PRE_BACKUP_NAME=<"!LATEST_FILE!"

call :restore_latest_pointer
set "LATEST_RESTORE_EXIT=!ERRORLEVEL!"

if not "!LATEST_RESTORE_EXIT!"=="0" (
    echo [ERRO] Nao foi possivel restaurar LATEST.txt ao estado anterior.
    goto :failed_while_running
)

if not "!PRE_BACKUP_EXIT!"=="0" (
    echo [ERRO] A restauracao foi cancelada porque o backup pre-restauracao falhou.
    goto :failed_while_running
)

set "PRE_BACKUP_FILE=%BACKUP_DIR%\!PRE_BACKUP_NAME!"
if not defined PRE_BACKUP_NAME (
    echo [ERRO] O backup pre-restauracao nao foi informado pelo backup-kanban.bat.
    goto :failed_while_running
)
if not exist "!PRE_BACKUP_FILE!" (
    echo [ERRO] O backup pre-restauracao nao foi localizado.
    goto :failed_while_running
)
if not exist "!PRE_BACKUP_FILE!.sha256" (
    echo [ERRO] Sidecar do backup pre-restauracao nao foi localizado.
    goto :failed_while_running
)
if /I "!PRE_BACKUP_FILE!"=="!BACKUP_FILE!" (
    echo [ERRO] O backup pre-restauracao colidiu com o backup selecionado.
    goto :failed_while_running
)

set "PRE_ACTUAL_HASH="
for /f "usebackq delims=" %%H in (`powershell -NoProfile -Command "(Get-FileHash -Algorithm SHA256 -LiteralPath '!PRE_BACKUP_FILE!').Hash.ToLowerInvariant()"`) do set "PRE_ACTUAL_HASH=%%H"
set "PRE_EXPECTED_HASH="
for /f "usebackq tokens=1" %%H in ("!PRE_BACKUP_FILE!.sha256") do set "PRE_EXPECTED_HASH=%%H"
if not defined PRE_ACTUAL_HASH (
    echo [ERRO] Nao foi possivel calcular o hash do backup pre-restauracao.
    goto :failed_while_running
)
if /I not "!PRE_ACTUAL_HASH!"=="!PRE_EXPECTED_HASH!" (
    echo [ERRO] Hash do backup pre-restauracao nao corresponde ao sidecar.
    goto :failed_while_running
)

echo [INFO] Preparando e validando a fonte de rollback antes de parar o Hermes...
docker compose cp "!PRE_BACKUP_FILE!" "%CONTAINER%:!ROLLBACK_SOURCE!" >nul
if errorlevel 1 goto :failed_while_running

docker compose exec -T -u 0:0 "%SERVICE%" sh -c "chown 10000:10000 '!ROLLBACK_SOURCE!' && chmod 600 '!ROLLBACK_SOURCE!'" >nul
if errorlevel 1 goto :failed_while_running

docker compose exec -T -u 10000:10000 "%SERVICE%" sh -c "printf '%%s' '%VALIDATE_PY%' | base64 -d | python3 - '!ROLLBACK_SOURCE!'" >"!VALIDATION!" 2>&1
if errorlevel 1 goto :failed_while_running
type "!VALIDATION!"

set "ROLLBACK_VOLUME_HASH="
for /f "usebackq tokens=1,* delims==" %%A in ("!VALIDATION!") do (
    if /I "%%A"=="sha256" set "ROLLBACK_VOLUME_HASH=%%B"
)
if not defined ROLLBACK_VOLUME_HASH (
    echo [ERRO] A validacao da fonte de rollback nao retornou SHA256.
    goto :failed_while_running
)
if /I not "!ROLLBACK_VOLUME_HASH!"=="!PRE_ACTUAL_HASH!" (
    echo [ERRO] A fonte de rollback no volume nao corresponde ao backup pre-restauracao.
    echo Hash Windows: !PRE_ACTUAL_HASH!
    echo Hash volume:  !ROLLBACK_VOLUME_HASH!
    goto :failed_while_running
)

echo [INFO] Parando o Hermes para restauracao offline...
docker compose stop "%SERVICE%" >nul
if errorlevel 1 (
    echo [ALERTA] docker compose stop retornou erro; verificando o estado real.
)

set "STILL_RUNNING="
for /f "usebackq delims=" %%I in (`docker inspect -f "{{.State.Running}}" "%CONTAINER%" 2^>nul`) do set "STILL_RUNNING=%%I"
if /I not "!STILL_RUNNING!"=="false" (
    echo [ERRO] Nao foi possivel confirmar que o Hermes esta parado.
    echo Estado observado: !STILL_RUNNING!
    docker compose up -d "%SERVICE%" >nul 2>&1
    goto :failed_while_running
)

echo [INFO] Substituindo o banco offline...
docker compose run --rm -T --no-deps --entrypoint sh -u 0:0 "%SERVICE%" -c "printf '%%s' '%RESTORE_PY%' | base64 -d | python3 - '!SOURCE!' '%DB%'" >"!VALIDATION!" 2>&1
if errorlevel 1 (
    type "!VALIDATION!"
    goto :rollback_required
)
type "!VALIDATION!"

echo [INFO] Reiniciando o Hermes...
docker compose up -d "%SERVICE%" >nul
if errorlevel 1 goto :rollback_required

powershell -NoProfile -Command "Start-Sleep -Seconds 15"

docker compose exec -T -u 10000:10000 "%SERVICE%" sh -c "printf '%%s' '%VALIDATE_PY%' | base64 -d | python3 - '%DB%'" >"!VALIDATION!" 2>&1
if errorlevel 1 (
    type "!VALIDATION!"
    goto :rollback_required
)
type "!VALIDATION!"

set "POST_JOURNAL="
set "POST_OWNER="
set "POST_MODE="
for /f "usebackq tokens=1,* delims==" %%A in ("!VALIDATION!") do (
    if /I "%%A"=="journal" set "POST_JOURNAL=%%B"
    if /I "%%A"=="owner" set "POST_OWNER=%%B"
    if /I "%%A"=="mode" set "POST_MODE=%%B"
)
if /I not "!POST_JOURNAL!"=="wal" (
    echo [ERRO] O banco restaurado nao esta em journal_mode WAL.
    echo Valor observado: !POST_JOURNAL!
    goto :rollback_required
)
if /I not "!POST_OWNER!"=="10000:10000" (
    echo [ERRO] Owner do banco restaurado esta incorreto.
    echo Valor observado: !POST_OWNER!
    goto :rollback_required
)
if /I not "!POST_MODE!"=="660" (
    echo [ERRO] Permissao do banco restaurado esta incorreta.
    echo Valor observado: !POST_MODE!
    goto :rollback_required
)

echo.
echo [OK] Restauracao concluida e validada.
echo Backup restaurado: !BACKUP_FILE!
echo Backup de seguranca anterior: !PRE_BACKUP_FILE!
set "EXIT_CODE=0"
goto :cleanup_running

:failed_while_running
echo.
echo [ERRO] A restauracao foi cancelada antes da substituicao offline.
echo O banco ativo nao foi alterado.
goto :cleanup_running

:rollback_required
echo.
echo [ERRO] A restauracao offline ou a validacao posterior falhou.
echo [INFO] O Hermes sera mantido parado durante o rollback.

docker compose stop "%SERVICE%" >nul 2>&1

docker compose run --rm -T --no-deps --entrypoint sh -u 0:0 "%SERVICE%" -c "printf '%%s' '%RESTORE_PY%' | base64 -d | python3 - '!ROLLBACK_SOURCE!' '%DB%'" >"!VALIDATION!" 2>&1
if errorlevel 1 (
    type "!VALIDATION!"
    goto :rollback_blocker
)

echo [INFO] Reiniciando o Hermes apos rollback...
docker compose up -d "%SERVICE%" >nul
if errorlevel 1 goto :rollback_blocker

powershell -NoProfile -Command "Start-Sleep -Seconds 15"

docker compose exec -T -u 10000:10000 "%SERVICE%" sh -c "printf '%%s' '%VALIDATE_PY%' | base64 -d | python3 - '%DB%'" >"!VALIDATION!" 2>&1
if errorlevel 1 (
    type "!VALIDATION!"
    docker compose stop "%SERVICE%" >nul 2>&1
    goto :rollback_blocker
)
type "!VALIDATION!"

set "ROLLBACK_JOURNAL="
set "ROLLBACK_OWNER="
set "ROLLBACK_MODE="
for /f "usebackq tokens=1,* delims==" %%A in ("!VALIDATION!") do (
    if /I "%%A"=="journal" set "ROLLBACK_JOURNAL=%%B"
    if /I "%%A"=="owner" set "ROLLBACK_OWNER=%%B"
    if /I "%%A"=="mode" set "ROLLBACK_MODE=%%B"
)
if /I not "!ROLLBACK_JOURNAL!"=="wal" (
    echo [ERRO] Rollback com journal_mode inesperado: !ROLLBACK_JOURNAL!
    docker compose stop "%SERVICE%" >nul 2>&1
    goto :rollback_blocker
)
if /I not "!ROLLBACK_OWNER!"=="10000:10000" (
    echo [ERRO] Rollback com owner inesperado: !ROLLBACK_OWNER!
    docker compose stop "%SERVICE%" >nul 2>&1
    goto :rollback_blocker
)
if /I not "!ROLLBACK_MODE!"=="660" (
    echo [ERRO] Rollback com permissao inesperada: !ROLLBACK_MODE!
    docker compose stop "%SERVICE%" >nul 2>&1
    goto :rollback_blocker
)

echo [ALERTA] A restauracao solicitada falhou, mas o banco anterior foi restaurado e validado.
set "EXIT_CODE=1"
goto :cleanup_running

:rollback_blocker
echo.
echo [BLOQUEADOR] O rollback automatico falhou ou nao pode ser validado.
echo O Hermes permanecera parado. Nao use o Kanban ate recuperacao manual.
docker compose stop "%SERVICE%" >nul 2>&1
set "EXIT_CODE=1"
goto :finish

:cleanup_running
docker compose exec -T -u 0:0 "%SERVICE%" rm -f "!SOURCE!" "!ROLLBACK_SOURCE!" >nul 2>&1

:finish
del /q "!VALIDATION!" 2>nul
del /q "!INSPECT_JSON!" 2>nul
if defined LOCK_HELD rmdir "%LOCK_DIR%" >nul 2>&1
exit /b !EXIT_CODE!

:restore_latest_pointer
if "!LATEST_ORIGINALLY_PRESENT!"=="1" (
    if not exist "!LATEST_PRESERVE!" exit /b 1
    move /y "!LATEST_PRESERVE!" "!LATEST_FILE!" >nul
    if errorlevel 1 exit /b 1
) else (
    del /q "!LATEST_FILE!" >nul 2>&1
    if exist "!LATEST_FILE!" exit /b 1
    del /q "!LATEST_PRESERVE!" >nul 2>&1
)

set "LATEST_PRESERVE="
exit /b 0
