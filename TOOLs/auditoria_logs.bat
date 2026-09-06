@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

REM Standard: always load config_env.bat before any operational logic.
if not exist "%SCRIPT_DIR%config_env.bat" (
    echo ERROR: config_env.bat not found:
    echo   %SCRIPT_DIR%config_env.bat
    exit /b 1
)

call "%SCRIPT_DIR%config_env.bat"
if errorlevel 1 (
    echo ERROR: config_env.bat failed.
    exit /b 1
)

if not exist "%SCRIPT_DIR%auditoria_logs.ps1" (
    echo ERROR: auditoria_logs.ps1 not found:
    echo   %SCRIPT_DIR%auditoria_logs.ps1
    exit /b 1
)

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%SCRIPT_DIR%auditoria_logs.ps1"
set "RC=%ERRORLEVEL%"

endlocal & exit /b %RC%
