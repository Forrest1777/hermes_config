@echo off
setlocal

REM ============================================================
REM Hermes Backup Script - compacto por padrao
REM
REM Execute este arquivo a partir da pasta raiz do Hermes:
REM   E:\dev\ai_agents\hermes
REM
REM MODOS:
REM
REM   backup-hermes.bat
REM     Backup COMPACTO recomendado para uso diario.
REM     Inclui:
REM       - hermes-data inteiro (exceto caches reconstruiveis)
REM       - compose inteiro
REM       - workspace principal
REM     Exclui:
REM       - workspace\skill_system_framework\.worktrees
REM       - caches .godot do checkout principal
REM
REM     Isso preserva:
REM       - config.yaml
REM       - profiles
REM       - plugins
REM       - bancos/estado persistente do Hermes
REM       - commits/branches locais do repositorio principal (.git)
REM       - alteracoes do checkout principal
REM       - docker-compose/.env/preflight e demais arquivos de compose
REM
REM   backup-hermes.bat full
REM     Backup COMPLETO.
REM     Inclui tambem .worktrees para preservar checkpoints/dirty
REM     states locais. Pode ficar MUITO maior.
REM
REM IMPORTANTE:
REM   - O Hermes e parado antes do tar para manter bancos/estado
REM     persistente consistentes.
REM   - O Hermes e iniciado novamente mesmo se o backup falhar.
REM   - GWRM fica fora da pasta Hermes e NAO faz parte deste backup.
REM ============================================================

cd /d "%~dp0"

set "ROOT=%CD%"
set "COMPOSE_FILE=.\compose\docker-compose.yml"
set "DATA_DIR=hermes-data"
set "COMPOSE_DIR=compose"
set "WORKSPACE_DIR=workspace"
set "BACKUP_DIR=backups"
set "MODE=compact"

if not "%~1"=="" (
    if /I "%~1"=="full" (
        set "MODE=full"
    ) else (
        echo.
        echo ERRO: Parametro desconhecido: %~1
        echo.
        echo Uso:
        echo   backup-hermes.bat
        echo   backup-hermes.bat full
        echo.
        pause
        exit /b 2
    )
)

REM ------------------------------------------------------------
REM Validacoes basicas
REM ------------------------------------------------------------

if not exist "%COMPOSE_FILE%" (
    echo ERRO: Nao encontrei "%COMPOSE_FILE%".
    echo Execute este .bat a partir da pasta raiz do Hermes.
    pause
    exit /b 1
)

if not exist ".\%DATA_DIR%" (
    echo ERRO: Nao encontrei ".\%DATA_DIR%".
    pause
    exit /b 1
)

if not exist ".\%COMPOSE_DIR%" (
    echo ERRO: Nao encontrei ".\%COMPOSE_DIR%".
    pause
    exit /b 1
)

if not exist ".\%WORKSPACE_DIR%" (
    echo ERRO: Nao encontrei ".\%WORKSPACE_DIR%".
    pause
    exit /b 1
)

if not exist ".\%DATA_DIR%\config.yaml" (
    echo ERRO: Nao encontrei ".\%DATA_DIR%\config.yaml".
    pause
    exit /b 1
)

if not exist ".\%COMPOSE_DIR%\hermes-config-preflight.py" (
    echo ERRO: Nao encontrei ".\%COMPOSE_DIR%\hermes-config-preflight.py".
    echo A protecao fail-fast OP-11 nao seria preservada.
    pause
    exit /b 1
)

REM Avisos das correcoes operacionais atuais.
if not exist ".\%DATA_DIR%\plugins\gwrm-lifecycle-cleanup" (
    echo AVISO: plugin gwrm-lifecycle-cleanup nao encontrado.
)

if not exist ".\%DATA_DIR%\plugins\retry-checkpoint-guard" (
    echo AVISO: plugin retry-checkpoint-guard nao encontrado.
)

if not exist ".\%DATA_DIR%\plugins\operational-block-completion-guard" (
    echo AVISO: plugin operational-block-completion-guard nao encontrado.
)

if not exist ".\%BACKUP_DIR%" (
    mkdir ".\%BACKUP_DIR%"
    if errorlevel 1 (
        echo ERRO: Nao foi possivel criar ".\%BACKUP_DIR%".
        pause
        exit /b 1
    )
)

for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd_HHmmss"') do set "DATE=%%i"

set "BACKUP_NAME=hermes-backup-%MODE%-%DATE%.tar.gz"
set "BACKUP_FILE=.\%BACKUP_DIR%\%BACKUP_NAME%"
set "BACKUP_FILE_LINUX=%BACKUP_DIR%/%BACKUP_NAME%"
set "BACKUP_EXIT=0"
set "VERIFY_EXIT=0"
set "HERMES_STOPPED=0"

echo.
echo ============================================================
echo Hermes Backup
echo ============================================================
echo Modo: %MODE%
echo Arquivo: %BACKUP_FILE%
echo.

if /I "%MODE%"=="compact" (
    echo Conteudo principal:
    echo   + hermes-data
    echo   + compose
    echo   + workspace
    echo   - workspace/skill_system_framework/.worktrees
    echo   - workspace/skill_system_framework/.godot
    echo.
    echo O modo compacto NAO preserva alteracoes nao commitadas
    echo existentes dentro de worktrees de cards.
    echo.
) else (
    echo Conteudo principal:
    echo   + hermes-data
    echo   + compose
    echo   + workspace COMPLETO
    echo.
    echo ATENCAO: o modo full pode gerar arquivos muito grandes.
    echo.
)

echo Caches sempre excluidos:
echo   - %DATA_DIR%/home/.cache
echo   - %DATA_DIR%/home/.npm
echo   - %DATA_DIR%/home/.pnpm-store
echo   - %DATA_DIR%/home/.local/share/uv
echo.

REM ------------------------------------------------------------
REM Para Hermes para obter snapshot consistente
REM ------------------------------------------------------------

echo Parando o Hermes...
docker compose -f "%COMPOSE_FILE%" stop
if errorlevel 1 (
    echo.
    echo ERRO: Falha ao parar o Hermes.
    pause
    exit /b 1
)

set "HERMES_STOPPED=1"

REM ------------------------------------------------------------
REM Cria o arquivo
REM ------------------------------------------------------------

echo.
echo Criando backup via container Linux temporario...

if /I "%MODE%"=="compact" (
    docker run --rm ^
      --mount "type=bind,source=%ROOT%,target=/host" ^
      -w /host ^
      alpine:3.20 ^
      sh -lc "tar --exclude='%DATA_DIR%/home/.cache' --exclude='%DATA_DIR%/home/.npm' --exclude='%DATA_DIR%/home/.pnpm-store' --exclude='%DATA_DIR%/home/.local/share/uv' --exclude='%WORKSPACE_DIR%/skill_system_framework/.godot' --exclude='%WORKSPACE_DIR%/skill_system_framework/.godot/*' --exclude='%WORKSPACE_DIR%/skill_system_framework/.worktrees' --exclude='%WORKSPACE_DIR%/skill_system_framework/.worktrees/*' -czf '%BACKUP_FILE_LINUX%' '%DATA_DIR%' '%COMPOSE_DIR%' '%WORKSPACE_DIR%'"
) else (
    docker run --rm ^
      --mount "type=bind,source=%ROOT%,target=/host" ^
      -w /host ^
      alpine:3.20 ^
      sh -lc "tar --exclude='%DATA_DIR%/home/.cache' --exclude='%DATA_DIR%/home/.npm' --exclude='%DATA_DIR%/home/.pnpm-store' --exclude='%DATA_DIR%/home/.local/share/uv' -czf '%BACKUP_FILE_LINUX%' '%DATA_DIR%' '%COMPOSE_DIR%' '%WORKSPACE_DIR%'"
)

set "BACKUP_EXIT=%ERRORLEVEL%"

if not "%BACKUP_EXIT%"=="0" (
    echo.
    echo ERRO: Falha ao criar o backup. Codigo: %BACKUP_EXIT%
    if exist "%BACKUP_FILE%" (
        echo Removendo backup parcial...
        del /f /q "%BACKUP_FILE%" >nul 2>&1
    )
    goto :RESTART_HERMES_ERROR
)

if not exist "%BACKUP_FILE%" (
    echo.
    echo ERRO: O arquivo de backup nao foi criado.
    goto :RESTART_HERMES_ERROR
)

for %%A in ("%BACKUP_FILE%") do set "BACKUP_SIZE=%%~zA"

if "%BACKUP_SIZE%"=="0" (
    echo.
    echo ERRO: O backup foi criado com tamanho zero.
    del /f /q "%BACKUP_FILE%" >nul 2>&1
    goto :RESTART_HERMES_ERROR
)

REM ------------------------------------------------------------
REM Valida o tar antes de reiniciar Hermes
REM ------------------------------------------------------------

echo.
echo Validando integridade e arquivos essenciais...

docker run --rm ^
  --mount "type=bind,source=%ROOT%,target=/host" ^
  -w /host ^
  alpine:3.20 ^
  sh -lc "tar -tzf '%BACKUP_FILE_LINUX%' > /tmp/hermes-backup-list.txt && grep -qx '%DATA_DIR%/config.yaml' /tmp/hermes-backup-list.txt && grep -qx '%COMPOSE_DIR%/docker-compose.yml' /tmp/hermes-backup-list.txt && grep -qx '%COMPOSE_DIR%/hermes-config-preflight.py' /tmp/hermes-backup-list.txt && grep -q '^%DATA_DIR%/plugins/gwrm-lifecycle-cleanup/' /tmp/hermes-backup-list.txt && grep -q '^%DATA_DIR%/plugins/retry-checkpoint-guard/' /tmp/hermes-backup-list.txt && grep -q '^%DATA_DIR%/plugins/operational-block-completion-guard/' /tmp/hermes-backup-list.txt"

set "VERIFY_EXIT=%ERRORLEVEL%"

if not "%VERIFY_EXIT%"=="0" (
    echo.
    echo ERRO: O tar foi criado, mas a verificacao de integridade/conteudo falhou.
    echo O arquivo sera removido para evitar um falso backup.
    del /f /q "%BACKUP_FILE%" >nul 2>&1
    goto :RESTART_HERMES_ERROR
)

REM ------------------------------------------------------------
REM Reinicia Hermes
REM ------------------------------------------------------------

echo.
echo Iniciando o Hermes novamente...
docker compose -f "%COMPOSE_FILE%" start

if errorlevel 1 (
    echo.
    echo AVISO: Backup criado e validado, mas houve falha ao reiniciar o Hermes.
    echo Backup preservado:
    echo %BACKUP_FILE%
    pause
    exit /b 1
)

set "HERMES_STOPPED=0"

REM ------------------------------------------------------------
REM Resultado
REM ------------------------------------------------------------

for /f %%i in ('powershell -NoProfile -Command "$p=(Resolve-Path ''%BACKUP_FILE%''); $s=(Get-Item $p).Length; ''{0:N2} MB'' -f ($s/1MB)"') do set "BACKUP_SIZE_MB=%%i"

echo.
echo ============================================================
echo BACKUP CONCLUIDO COM SUCESSO
echo ============================================================
echo Modo: %MODE%
echo Arquivo:
echo %BACKUP_FILE%
echo.
echo Tamanho:
echo %BACKUP_SIZE% bytes
echo %BACKUP_SIZE_MB%
echo.
echo Verificacao:
echo   [OK] tar legivel
echo   [OK] hermes-data/config.yaml
echo   [OK] compose/docker-compose.yml
echo   [OK] compose/hermes-config-preflight.py
echo   [OK] gwrm-lifecycle-cleanup
echo   [OK] retry-checkpoint-guard
echo   [OK] operational-block-completion-guard
echo.

if /I "%MODE%"=="compact" (
    echo Para preservar tambem worktrees/checkpoints locais:
    echo   backup-hermes.bat full
    echo.
)

pause
endlocal
exit /b 0


:RESTART_HERMES_ERROR
echo.
echo Tentando iniciar o Hermes novamente...

if "%HERMES_STOPPED%"=="1" (
    docker compose -f "%COMPOSE_FILE%" start
)

echo.
echo Backup NAO concluido.
pause
endlocal
exit /b 1
