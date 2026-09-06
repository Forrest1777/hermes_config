@echo off
REM Central path configuration for Hermes audit/cleanup scripts.
REM Keep this file in the same directory as the operational scripts.

set "HERMES_COMPOSE_FILE=E:\dev\ai_agents\hermes\compose\docker-compose.yml"
set "HERMES_DOCKER_SERVICE=hermes"

set "HERMES_LOGS_ROOT=E:\dev\ai_agents\hermes\logs"
set "HERMES_WORKERS_LOG_DIR=%HERMES_LOGS_ROOT%\workers"
set "HERMES_RUNTIME_LOG_DIR=%HERMES_LOGS_ROOT%\runtime"
set "HERMES_KANBAN_LOG_DIR=%HERMES_LOGS_ROOT%\kanban"
set "HERMES_KANBAN_DB_CONTAINER_PATH=/var/lib/hermes-kanban/kanban.db"
set "HERMES_AUDIT_LOG_DIR=%HERMES_LOGS_ROOT%\auditoria"

set "GWRM_LOG_FILE=E:\dev\ai_agents\GWRM\logs\gwrm.log"

exit /b 0
