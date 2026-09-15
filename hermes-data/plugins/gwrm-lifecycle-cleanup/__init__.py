import json
import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

from hermes_cli.config import load_config_readonly


LOG = logging.getLogger("hermes.plugins.gwrm_lifecycle_cleanup")

_OPENER = build_opener(ProxyHandler({}))

_RECONCILE_LOCK = threading.Lock()
_LAST_RECONCILE = 0.0
_RECONCILE_INTERVAL_SECONDS = 30.0


def _gwrm_connection():
    config = load_config_readonly() or {}

    lsp = config.get("lsp") or {}
    servers = lsp.get("servers") or {}
    godot = servers.get("godot-gdscript") or {}
    env = godot.get("env") or {}

    control_url = str(
        env.get("GWRM_CONTROL_URL") or ""
    ).strip().rstrip("/")

    # HERMES_GWRM_LIFECYCLE_ENV_CANONICAL_2026_09_14
    # The raw YAML is validated separately by hermes-config-preflight.py.
    # Runtime authentication uses the container process environment so every
    # GWRM integration shares one effective credential source.
    api_key = str(
        os.environ.get("GWRM_API_KEY") or ""
    ).strip()

    if not control_url:
        raise RuntimeError(
            "GWRM_CONTROL_URL missing from godot-gdscript env"
        )

    if not api_key:
        raise RuntimeError(
            "GWRM_API_KEY missing from godot-gdscript env"
        )

    return control_url, api_key


def _request_json(method, path, timeout=30.0):
    control_url, api_key = _gwrm_connection()

    request = Request(
        f"{control_url}{path}",
        data=b"" if method == "POST" else None,
        method=method,
        headers={
            "X-API-Key": api_key,
            "Accept": "application/json",
        },
    )

    with _OPENER.open(request, timeout=timeout) as response:
        raw = response.read()

    if not raw:
        return {}

    return json.loads(raw.decode("utf-8"))



# HERMES_GWRM_GUT_EVENT_LEASE_2026_09_14
_GUT_EVENT_STATE_DB = Path(
    "/opt/data/logs/gwrm-gut-event-runner/state-v1.sqlite3"
)


def _active_gut_event_lease(task_id):
    if not isinstance(task_id, str) or not task_id.strip():
        return None
    if not _GUT_EVENT_STATE_DB.exists():
        return None

    try:
        conn = sqlite3.connect(str(_GUT_EVENT_STATE_DB), timeout=5)
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT w.operation_id,w.run_id,w.state,w.updated_at "
            "FROM waits w LEFT JOIN terminal_events e "
            "ON e.operation_id=w.operation_id "
            "WHERE w.task_id=? "
            "AND w.state IN ('registered','parked') "
            "AND e.operation_id IS NULL "
            "ORDER BY w.updated_at DESC LIMIT 1",
            (task_id.strip(),),
        ).fetchone()
        conn.close()

        if row is None:
            return None

        return {
            "operation_id": str(row["operation_id"]),
            "run_id": int(row["run_id"]),
            "state": str(row["state"]),
            "updated_at": int(row["updated_at"]),
        }

    except Exception as exc:
        # Fail closed: if ownership cannot be proven safe, never kill a
        # potentially active event-driven GUT process.
        LOG.warning(
            "[GWRM_LIFECYCLE] GUT lease probe failed; cleanup deferred "
            "task=%s error=%s",
            task_id,
            exc,
        )
        return {
            "operation_id": None,
            "run_id": None,
            "state": "probe_failed",
            "updated_at": None,
        }


def _cleanup_deferred_by_gut_lease(task_id, event_name):
    lease = _active_gut_event_lease(task_id)
    if lease is None:
        return False

    LOG.info(
        "[GWRM_LIFECYCLE] cleanup deferred by active GUT lease "
        "event=%s task=%s operation_id=%s run_id=%s state=%s",
        event_name,
        task_id,
        lease.get("operation_id"),
        lease.get("run_id"),
        lease.get("state"),
    )
    return True


def _deactivate(task_id, event_name):
    if not isinstance(task_id, str) or not task_id.strip():
        return False

    task_id = task_id.strip()

    if _cleanup_deferred_by_gut_lease(task_id, event_name):
        return True

    try:
        payload = _request_json(
            "POST",
            "/api/v1/worktrees/"
            + quote(task_id, safe="")
            + "/deactivate",
        )
    except HTTPError as exc:
        LOG.warning(
            "[GWRM_LIFECYCLE] cleanup failed "
            "event=%s task=%s http_status=%s",
            event_name,
            task_id,
            exc.code,
        )
        return False
    except Exception as exc:
        LOG.warning(
            "[GWRM_LIFECYCLE] cleanup failed "
            "event=%s task=%s error=%s",
            event_name,
            task_id,
            exc,
        )
        return False

    LOG.info(
        "[GWRM_LIFECYCLE] cleanup requested "
        "event=%s task=%s status=%s "
        "desired_active=%s directory_released=%s",
        event_name,
        task_id,
        payload.get("status"),
        payload.get("desired_active"),
        payload.get("directory_released"),
    )

    return True


def _event_handler(event_name):
    def handler(task_id=None, **kwargs):
        return _deactivate(task_id, event_name)

    handler.__name__ = (
        "gwrm_cleanup_" + event_name
    )

    return handler


def _needs_reconciliation(record):
    if record.get("desired_active"):
        return True

    if record.get("status") == "failed":
        return True

    if record.get("residual_pids"):
        return True

    if (
        record.get("status") == "stopped"
        and record.get("directory_released") is False
    ):
        return True

    return False


def _reconcile_dispatcher(
    board=None,
    dry_run=False,
    **kwargs,
):
    global _LAST_RECONCILE

    if dry_run:
        return None

    now = time.monotonic()

    with _RECONCILE_LOCK:
        if (
            now - _LAST_RECONCILE
            < _RECONCILE_INTERVAL_SECONDS
        ):
            return None

        _LAST_RECONCILE = now

    try:
        payload = _request_json(
            "GET",
            "/api/v1/worktrees",
            timeout=10.0,
        )
    except Exception as exc:
        LOG.warning(
            "[GWRM_LIFECYCLE] reconciliation query failed: %s",
            exc,
        )
        return False

    records = payload.get("worktrees") or []

    candidates = [
        record
        for record in records
        if isinstance(record, dict)
        and _needs_reconciliation(record)
    ]

    if not candidates:
        return True

    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        # HERMES_KANBAN_CONNECT_CLOSING_MIGRATION_2026_09_14
        with kbc.connect_closing(board=board) as conn:
            for record in candidates:
                task_id = record.get("worktree_name")

                if not isinstance(task_id, str):
                    continue

                if not task_id.startswith("t_"):
                    continue

                task = kb.get_task(conn, task_id)

                if task is None:
                    continue

                if task.status == "running":
                    continue

                _deactivate(
                    task_id,
                    f"dispatcher_reconcile:{task.status}",
                )

    except Exception as exc:
        LOG.warning(
            "[GWRM_LIFECYCLE] reconciliation failed: %s",
            exc,
        )
        return False

    return True


def register(ctx):
    ctx.register_hook(
        "kanban_task_completed",
        _event_handler("kanban_task_completed"),
    )

    ctx.register_hook(
        "kanban_task_blocked",
        _event_handler("kanban_task_blocked"),
    )

    ctx.register_hook(
        "on_kanban_worker_exited",
        _event_handler("on_kanban_worker_exited"),
    )

    ctx.register_hook(
        "on_kanban_worker_stale_claim",
        _event_handler("on_kanban_worker_stale_claim"),
    )

    ctx.register_hook(
        "on_kanban_dispatch_tick",
        _reconcile_dispatcher,
    )
