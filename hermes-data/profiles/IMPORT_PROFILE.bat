@echo off
setlocal EnableExtensions

REM ============================================================
REM IMPORT_PROFILE.bat
REM Faz o caminho inverso do EXTRACT_PROFILE.bat:
REM copia os arquivos prefixados da pasta atual para cada profile,
REM sobrescrevendo SOUL.md e config.yaml.
REM ============================================================

set "BASE=%~dp0"
set "ERRORS=0"

call :import_profile "execution-governor"
call :import_profile "implementation-architect"
call :import_profile "implementation-orchestrator"
call :import_profile "implementation-worker"

echo.
if "%ERRORS%"=="0" (
    echo [OK] Importacao concluida com sucesso.
    exit /b 0
) else (
    echo [AVISO] Importacao concluida com %ERRORS% erro(s).
    exit /b 1
)

:import_profile
set "PROFILE=%~1"

if not exist "%BASE%%PROFILE%\" (
    echo [ERRO] Diretorio nao encontrado: "%BASE%%PROFILE%\"
    set /a ERRORS+=1
    exit /b 0
)

if not exist "%BASE%%PROFILE%_SOUL.md" (
    echo [ERRO] Arquivo nao encontrado: "%BASE%%PROFILE%_SOUL.md"
    set /a ERRORS+=1
) else (
    copy /Y "%BASE%%PROFILE%_SOUL.md" "%BASE%%PROFILE%\SOUL.md" >nul
    if errorlevel 1 (
        echo [ERRO] Falha ao atualizar SOUL.md de "%PROFILE%".
        set /a ERRORS+=1
    ) else (
        echo [OK] %PROFILE%_SOUL.md ^> %PROFILE%\SOUL.md
    )
)

if not exist "%BASE%%PROFILE%_config.yaml" (
    echo [ERRO] Arquivo nao encontrado: "%BASE%%PROFILE%_config.yaml"
    set /a ERRORS+=1
) else (
    copy /Y "%BASE%%PROFILE%_config.yaml" "%BASE%%PROFILE%\config.yaml" >nul
    if errorlevel 1 (
        echo [ERRO] Falha ao atualizar config.yaml de "%PROFILE%".
        set /a ERRORS+=1
    ) else (
        echo [OK] %PROFILE%_config.yaml ^> %PROFILE%\config.yaml
    )
)

exit /b 0
