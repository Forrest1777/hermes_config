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

echo.
echo ============================================================
echo  LOG CLEANUP - DESTRUCTIVE OPERATION
echo ============================================================
echo.
echo The following logs will be deleted:
echo.
echo   1. Workers:
echo      %HERMES_WORKERS_LOG_DIR%
echo.
echo   2. GWRM:
echo      %GWRM_LOG_FILE%
echo.
echo   3. Hermes runtime:
echo      %HERMES_RUNTIME_LOG_DIR%
echo.
echo The following will NOT be deleted:
echo   - %HERMES_KANBAN_LOG_DIR%
echo   - %HERMES_AUDIT_LOG_DIR%
echo.
echo Type exactly DELETE to continue.
echo.
set "CONFIRM="
set /p "CONFIRM=Confirmation: "

if not "%CONFIRM%"=="DELETE" (
    echo.
    echo Confirmation failed. Nothing was deleted.
    endlocal
    exit /b 0
)

echo.
echo Cleaning worker logs...
if exist "%HERMES_WORKERS_LOG_DIR%" (
    del /f /q "%HERMES_WORKERS_LOG_DIR%\*" >nul 2>&1
    for /d %%D in ("%HERMES_WORKERS_LOG_DIR%\*") do rd /s /q "%%D"
) else (
    echo WARNING: Directory not found: %HERMES_WORKERS_LOG_DIR%
)

echo Cleaning Hermes runtime logs...
if exist "%HERMES_RUNTIME_LOG_DIR%" (
    del /f /q "%HERMES_RUNTIME_LOG_DIR%\*" >nul 2>&1
    for /d %%D in ("%HERMES_RUNTIME_LOG_DIR%\*") do rd /s /q "%%D"
) else (
    echo WARNING: Directory not found: %HERMES_RUNTIME_LOG_DIR%
)

echo Recreating persistent runtime log directories...
if not exist "%HERMES_RUNTIME_LOG_DIR%" (
    mkdir "%HERMES_RUNTIME_LOG_DIR%"
)

if not exist "%HERMES_RUNTIME_LOG_DIR%\git-trace2" (
    mkdir "%HERMES_RUNTIME_LOG_DIR%\git-trace2"
)

if not exist "%HERMES_RUNTIME_LOG_DIR%\orchestration-control" (
    mkdir "%HERMES_RUNTIME_LOG_DIR%\orchestration-control"
)

echo Cleaning GWRM log...
if exist "%GWRM_LOG_FILE%" (
    del /f /q "%GWRM_LOG_FILE%"
) else (
    echo WARNING: File not found: %GWRM_LOG_FILE%
)

echo.
echo ============================================================
echo  Cleanup completed.
echo ============================================================
echo.

endlocal
exit /b 0
