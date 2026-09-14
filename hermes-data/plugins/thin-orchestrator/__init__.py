"""Thin deterministic state snapshot for the Hermes implementation orchestrator.

Read-only state compression for bootstrap/resume. No Kanban/Git mutation.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

NAME = "thin-orchestrator"
VERSION = "1.0.0"
TOOLSET = "thin_orchestrator"
MARKER = "HERMES_THIN_ORCHESTRATOR_TODO4_2026_09_13"
ALLOWED_PROFILES = {"implementation-orchestrator"}

SCHEMA = {
    "name": "orchestrator_compact_state",
    "description": (
        "Return one bounded read-only snapshot of the current/root Kanban card: "
        "task facts, prerequisite/dependent states, recent lifecycle events, "
        "checkpoint/handoff markers, and root-worktree Git facts."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "Defaults to HERMES_KANBAN_TASK."},
            "recent_events": {"type": "integer", "minimum": 1, "maximum": 30},
            "recent_comments": {"type": "integer", "minimum": 1, "maximum": 30},
        },
        "additionalProperties": False,
    },
}

def _available() -> bool:
    return str(os.environ.get("HERMES_PROFILE") or "") in ALLOWED_PROFILES

def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)

def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False)

def _cfg() -> dict[str, int]:
    out = {"recent_events": 12, "recent_comments": 12, "max_marker_chars": 1800}
    try:
        from hermes_cli.config import load_config
        raw = (load_config() or {}).get("thin_orchestrator") or {}
        if isinstance(raw, dict):
            for key in tuple(out):
                if key in raw:
                    out[key] = int(raw[key])
    except Exception:
        pass
    out["recent_events"] = max(1, min(30, out["recent_events"]))
    out["recent_comments"] = max(1, min(30, out["recent_comments"]))
    out["max_marker_chars"] = max(400, min(5000, out["max_marker_chars"]))
    return out

def _table_exists(conn, table: str) -> bool:
    return bool(conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone())

def _columns(conn, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")')}
    except Exception:
        return set()

def _dict_row(row: Any) -> dict[str, Any]:
    if row is None:
        return {}
    try:
        return {key: row[key] for key in row.keys()}
    except Exception:
        return {}

def _task(conn, task_id: str) -> dict[str, Any] | None:
    cols = _columns(conn, "tasks")
    wanted = [
        c for c in (
            "id", "title", "status", "assignee", "current_run_id", "created_at",
            "updated_at", "claim_expires", "consecutive_failures",
            "last_failure_error", "block_kind", "block_recurrences",
        ) if c in cols
    ]
    if "id" not in wanted:
        return None
    row = conn.execute(
        "SELECT " + ", ".join(wanted) + " FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    return _dict_row(row) if row else None

def _linked_tasks(conn, task_id: str) -> dict[str, list[dict[str, Any]]]:
    out = {"prerequisites": [], "dependents": []}
    if not _table_exists(conn, "task_links"):
        return out
    for name, sql in (
        ("prerequisites", "SELECT parent_id AS linked_id FROM task_links WHERE child_id=? ORDER BY parent_id"),
        ("dependents", "SELECT child_id AS linked_id FROM task_links WHERE parent_id=? ORDER BY child_id"),
    ):
        for row in conn.execute(sql, (task_id,)).fetchall():
            linked_id = str(row["linked_id"] if hasattr(row, "keys") else row[0])
            task = _task(conn, linked_id) or {"id": linked_id, "status": "missing"}
            out[name].append({
                key: task.get(key)
                for key in ("id", "title", "status", "assignee", "current_run_id")
                if key in task
            })
    return out

def _recent_events(conn, task_id: str, limit: int) -> list[dict[str, Any]]:
    if not _table_exists(conn, "task_events"):
        return []
    cols = _columns(conn, "task_events")
    wanted = [c for c in ("id", "kind", "run_id", "created_at", "payload") if c in cols]
    if "kind" not in wanted:
        return []
    order = "id DESC" if "id" in cols else "rowid DESC"
    rows = conn.execute(
        "SELECT " + ", ".join(wanted) +
        f" FROM task_events WHERE task_id=? ORDER BY {order} LIMIT ?",
        (task_id, int(limit)),
    ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        item = _dict_row(row)
        raw = item.pop("payload", None)
        if raw:
            try:
                payload = json.loads(str(raw))
            except Exception:
                payload = {}
            if isinstance(payload, dict):
                keep = {}
                for key in ("status", "resume_status", "retry_status", "source_status",
                            "reason", "error", "operation_id"):
                    if key in payload:
                        value = payload[key]
                        keep[key] = str(value)[:500] if value is not None else None
                if keep:
                    item["payload"] = keep
        out.append(item)
    return out

_CHECKPOINT_KEYS = (
    "phase_id", "status", "integration_head", "architecture_revision",
    "code_head", "docs_head", "delivery_target_branch",
    "main_integration_pending", "main_integrated", "push_performed",
)

def _checkpoint_fields(body: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    version = re.search(r"OPERATIONAL_CHECKPOINT_CANONICAL\s+v(\d+)", body or "")
    if version:
        out["version"] = int(version.group(1))
    for key in _CHECKPOINT_KEYS:
        match = re.search(rf"(?mi)^\s*{re.escape(key)}\s*:\s*([^\r\n#]+)", body or "")
        if match:
            out[key] = match.group(1).strip().strip("'\"")
    return out

def _comment_markers(conn, task_id: str, limit: int, max_chars: int) -> dict[str, Any]:
    result: dict[str, Any] = {"checkpoint": None, "markers": []}
    if not _table_exists(conn, "task_comments"):
        return result
    cols = _columns(conn, "task_comments")
    if not {"task_id", "body"}.issubset(cols):
        return result
    wanted = [c for c in ("id", "author", "body", "created_at") if c in cols]
    order = "id DESC" if "id" in cols else "rowid DESC"
    rows = conn.execute(
        "SELECT " + ", ".join(wanted) +
        f" FROM task_comments WHERE task_id=? ORDER BY {order} LIMIT ?",
        (task_id, int(max(limit * 4, 20))),
    ).fetchall()

    interesting = (
        "OPERATIONAL_CHECKPOINT_CANONICAL", "READY_FOR_INTEGRATION",
        "GWRM_GUT_TERMINAL_EVENT", "ORCHESTRATOR_MATERIALIZATION",
        "HUMAN_AUTHORIZATION_EXHAUSTED_RECOVERY", "DIRTY_CHECKPOINT_RECOVERY",
    )
    for row in rows:
        item = _dict_row(row)
        body = str(item.pop("body", "") or "")
        matched = [marker for marker in interesting if marker in body]
        if not matched:
            continue
        if result["checkpoint"] is None and "OPERATIONAL_CHECKPOINT_CANONICAL" in matched:
            result["checkpoint"] = {
                **_checkpoint_fields(body),
                "comment_id": item.get("id"),
                "created_at": item.get("created_at"),
            }
        first_lines = [line.strip() for line in body.splitlines() if line.strip()][:12]
        result["markers"].append({
            "id": item.get("id"),
            "author": item.get("author"),
            "created_at": item.get("created_at"),
            "markers": matched,
            "excerpt": "\n".join(first_lines)[:max_chars],
        })
        if len(result["markers"]) >= limit:
            break
    return result

def _git_state() -> dict[str, Any]:
    workspace = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if not workspace:
        return {"available": False, "reason": "HERMES_KANBAN_WORKSPACE missing"}
    path = Path(workspace)
    if not path.is_dir():
        return {"available": False, "reason": "workspace missing", "workspace": workspace}

    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, timeout=6, check=True,
        )
        return proc.stdout.strip()

    try:
        head = run("rev-parse", "HEAD")
        branch = run("branch", "--show-current")
        porcelain = run("status", "--porcelain=v1", "--untracked-files=normal")
        changed = [line for line in porcelain.splitlines() if line.strip()]
        return {
            "available": True, "workspace": workspace, "head": head,
            "branch": branch or None, "clean": not changed,
            "changed_count": len(changed), "changed_preview": changed[:12],
        }
    except Exception as exc:
        return {
            "available": False, "workspace": workspace,
            "reason": f"{type(exc).__name__}: {exc}"[:800],
        }

def _handler(args: dict[str, Any], **kwargs: Any) -> str:
    del kwargs
    if not _available():
        return _err("thin-orchestrator unavailable outside implementation-orchestrator")

    cfg = _cfg()
    task_id = str(args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return _err("task_id unavailable")
    recent_events = max(1, min(30, int(args.get("recent_events") or cfg["recent_events"])))
    recent_comments = max(1, min(30, int(args.get("recent_comments") or cfg["recent_comments"])))

    try:
        from hermes_cli.kanban_db_connect import connect_closing
        with connect_closing() as conn:
            task = _task(conn, task_id)
            if task is None:
                return _err("task not found", task_id=task_id)
            links = _linked_tasks(conn, task_id)
            events = _recent_events(conn, task_id, recent_events)
            comments = _comment_markers(conn, task_id, recent_comments, cfg["max_marker_chars"])
    except Exception as exc:
        return _err(f"{type(exc).__name__}: {exc}"[:1200], task_id=task_id)

    open_prereqs = [
        item["id"] for item in links["prerequisites"]
        if str(item.get("status") or "").lower() not in {"done", "archived"}
    ]
    snapshot = {
        "schema": "thin-orchestrator-state-v1",
        "marker": MARKER,
        "generated_at_epoch": int(time.time()),
        "task": task,
        "dependency_summary": {
            "prerequisite_count": len(links["prerequisites"]),
            "open_prerequisite_count": len(open_prereqs),
            "open_prerequisite_ids": open_prereqs,
            "dependent_count": len(links["dependents"]),
        },
        "prerequisites": links["prerequisites"],
        "dependents": links["dependents"],
        "recent_events": events,
        "checkpoint": comments["checkpoint"],
        "recent_markers": comments["markers"],
        "git": _git_state(),
        "usage": {"purpose": "single bounded bootstrap/resume fact snapshot",
                  "do_not_repeat_if_unchanged": True},
    }
    return _ok(snapshot=snapshot)

def register(ctx: Any) -> None:
    ctx.register_tool(
        name="orchestrator_compact_state",
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=_handler,
        check_fn=_available,
        emoji="🪶",
    )
