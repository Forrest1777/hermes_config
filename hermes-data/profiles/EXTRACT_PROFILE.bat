@echo off
setlocal EnableExtensions

REM ============================================================
REM EXTRACT_PROFILE.bat
REM Copia SOUL.md e config.yaml de cada profile para a pasta atual,
REM renomeando-os com o prefixo do nome do profile.
REM ============================================================

set "BASE=%~dp0"
set "ERRORS=0"

call :extract_profile "execution-governor"
call :extract_profile "implementation-architect"
call :extract_profile "implementation-orchestrator"
call :extract_profile "implementation-worker"

echo.
if "%ERRORS%"=="0" (
    echo [OK] Extracao concluida com sucesso.
    exit /b 0
) else (
    echo [AVISO] Extracao concluida com %ERRORS% erro(s).
    exit /b 1
)

:extract_profile
set "PROFILE=%~1"

if not exist "%BASE%%PROFILE%\SOUL.md" (
    echo [ERRO] Arquivo nao encontrado: "%BASE%%PROFILE%\SOUL.md"
    set /a ERRORS+=1
) else (
    copy /Y "%BASE%%PROFILE%\SOUL.md" "%BASE%%PROFILE%_SOUL.md" >nul
    if errorlevel 1 (
        echo [ERRO] Falha ao copiar SOUL.md de "%PROFILE%".
        set /a ERRORS+=1
    ) else (
        echo [OK] %PROFILE%\SOUL.md ^> %PROFILE%_SOUL.md
    )
)

if not exist "%BASE%%PROFILE%\config.yaml" (
    echo [ERRO] Arquivo nao encontrado: "%BASE%%PROFILE%\config.yaml"
    set /a ERRORS+=1
) else (
    copy /Y "%BASE%%PROFILE%\config.yaml" "%BASE%%PROFILE%_config.yaml" >nul
    if errorlevel 1 (
        echo [ERRO] Falha ao copiar config.yaml de "%PROFILE%".
        set /a ERRORS+=1
    ) else (
        echo [OK] %PROFILE%\config.yaml ^> %PROFILE%_config.yaml
    )
)

exit /b 0
