import json
import logging
import threading
import time
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

    api_key = str(
        env.get("GWRM_API_KEY") or ""
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


def _deactivate(task_id, event_name):
    if not isinstance(task_id, str) or not task_id.strip():
        return False

    task_id = task_id.strip()

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

        with kb.connect_closing(board=board) as conn:
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
