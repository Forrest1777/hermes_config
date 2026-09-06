"""Narrow cross-card administrative bridge for a dispatched implementation-orchestrator.

The native Kanban tool surface intentionally hides kanban_unblock from
dispatcher-owned workers. This plugin restores only the one operation required
by the orchestration protocol, with relationship and state validation.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any

TOOLSET = "orchestration_control"
ALLOWED_TARGET_PROFILES = {"implementation-architect", "implementation-worker"}

SCHEMA = {
    "name": "orchestration_unblock_card",
    "description": (
        "Safely reactivate one blocked/scheduled card belonging to the current "
        "implementation-orchestrator root. For implementation-architect targets, "
        "an ORCHESTRATOR_MATERIALIZATION comment with the approved operational "
        "fields must already exist. Never use this for unrelated cards."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_task_id": {
                "type": "string",
                "description": "Target Kanban task id (t_<hex>).",
            },
            "reason": {
                "type": "string",
                "description": "Short audit reason for the reactivation.",
            },
        },
        "required": ["target_task_id", "reason"],
        "additionalProperties": False,
    },
}

def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)

def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False)

def _available() -> bool:
    return (
        os.environ.get("HERMES_PROFILE") == "implementation-orchestrator"
        and bool(os.environ.get("HERMES_KANBAN_TASK"))
    )

def _related(conn, root_id: str, target_id: str, target_body: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM task_links
        WHERE (parent_id = ? AND child_id = ?)
           OR (parent_id = ? AND child_id = ?)
        LIMIT 1
        """,
        (root_id, target_id, target_id, root_id),
    ).fetchone()
    if row:
        return True

    root_re = re.escape(root_id)
    patterns = (
        rf"(?mi)^\s*logical_parent_card_id\s*:\s*[\"']?{root_re}[\"']?\s*$",
        rf"(?mi)^\s*parent_card_id\s*:\s*[\"']?{root_re}[\"']?\s*$",
        rf'["\']logical_parent_card_id["\']\s*:\s*["\']{root_re}["\']',
    )
    return any(re.search(p, target_body or "") for p in patterns)

def _architect_materialized(kb, conn, target_id: str, root_id: str) -> bool:
    required = (
        "ORCHESTRATOR_MATERIALIZATION",
        "root_task_id",
        "user_decision",
        "approved_bundle_id",
        "authorized_architecture_files",
        "protected_path_authorizations",
        "allowed_paths",
    )
    try:
        comments = kb.list_comments(conn, target_id)
    except Exception:
        return False
    for c in reversed(list(comments or [])):
        body = str(getattr(c, "body", "") or "")
        if all(token in body for token in required) and root_id in body:
            return True
    return False

def _audit(payload: dict[str, Any]) -> None:
    try:
        path = Path("/opt/data/logs/orchestration-control/events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": int(time.time()), **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        # Audit failure must not broaden authority; the Kanban comment below is
        # still a durable board-side audit trail.
        pass

def _handle(args: dict, **kwargs) -> str:
    del kwargs
    if not _available():
        return _err("orchestration-control unavailable outside a dispatched implementation-orchestrator")

    root_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    target_id = str(args.get("target_task_id") or "").strip()
    reason = str(args.get("reason") or "").strip()[:500]

    if not re.fullmatch(r"t_[0-9a-fA-F]+", target_id):
        return _err("invalid target_task_id")
    if not reason:
        return _err("reason is required")
    if target_id == root_id:
        return _err("target must be a different card; use native lifecycle tools for the root")

    try:
        from hermes_cli import kanban_db as kb
        board = os.environ.get("HERMES_KANBAN_BOARD") or None
        conn = kb.connect(board=board)
        try:
            root = kb.get_task(conn, root_id)
            target = kb.get_task(conn, target_id)
            if root is None or target is None:
                return _err("root or target task not found", root_task_id=root_id, target_task_id=target_id)
            if str(getattr(root, "assignee", "") or "") != "implementation-orchestrator":
                return _err("active root is not assigned to implementation-orchestrator")
            assignee = str(getattr(target, "assignee", "") or "")
            if assignee not in ALLOWED_TARGET_PROFILES:
                return _err("target assignee is outside the orchestration allowlist", assignee=assignee)
            status = str(getattr(target, "status", "") or "")
            if status not in {"blocked", "scheduled", "triage"}:
                return _err("target is not safely reactivateable", status=status)
            if getattr(target, "current_run_id", None) is not None:
                return _err("target still has current_run_id; refusing concurrent reactivation")
            if not _related(conn, root_id, target_id, str(getattr(target, "body", "") or "")):
                return _err("target is not linked/logically owned by the active root")

            if assignee == "implementation-architect" and not _architect_materialized(
                kb, conn, target_id, root_id
            ):
                return _err(
                    "architect target lacks canonical ORCHESTRATOR_MATERIALIZATION comment",
                    target_task_id=target_id,
                )

            if status == "triage":
                # Triage is accepted only for the architect path above, where
                # canonical materialization has already been verified. Native
                # unblock_task intentionally excludes triage, so use a narrow
                # CAS rather than a generic status mutator.
                if assignee != "implementation-architect":
                    return _err("triage reactivation is architect-only")
                now = int(time.time())
                status_payload = json.dumps(
                    {
                        "status": "ready",
                        "from": "triage",
                        "reason": reason,
                        "source": "orchestration-control",
                        "root_task_id": root_id,
                    },
                    ensure_ascii=False,
                )
                with kb.write_txn(conn):
                    cur = conn.execute(
                        """
                        UPDATE tasks
                        SET status='ready',
                            completed_at=NULL,
                            claim_lock=NULL,
                            claim_expires=NULL,
                            worker_pid=NULL,
                            current_run_id=NULL
                        WHERE id=?
                          AND status='triage'
                          AND current_run_id IS NULL
                        """,
                        (target_id,),
                    )
                    if cur.rowcount != 1:
                        return _err("triage->ready CAS lost", target_task_id=target_id)
                    conn.execute(
                        "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?, NULL, 'status', ?, ?)",
                        (target_id, status_payload, now),
                    )
                try:
                    kb.notify_task_updated(
                        conn,
                        target_id,
                        ["status", "completed_at", "claim_lock", "claim_expires",
                         "worker_pid", "current_run_id"],
                        board=board,
                    )
                except Exception:
                    pass
            else:
                changed = kb.unblock_task(conn, target_id)
                if not changed:
                    return _err("native unblock_task refused transition", target_task_id=target_id)

            audit_body = (
                "ORCHESTRATION_CONTROL_REACTIVATION\n"
                f"root_task_id: {root_id}\n"
                f"target_task_id: {target_id}\n"
                f"reason: {reason}\n"
            )
            try:
                kb.add_comment(
                    conn,
                    target_id,
                    author="orchestration-control",
                    body=audit_body,
                )
            except Exception:
                pass

            resulting = kb.get_task(conn, target_id)
            result_status = str(getattr(resulting, "status", "") or "")
            payload = {
                "event": "cross_card_unblock",
                "board": board or "default",
                "root_task_id": root_id,
                "target_task_id": target_id,
                "target_assignee": assignee,
                "prior_status": status,
                "resulting_status": result_status,
                "reason": reason,
            }
            _audit(payload)
            return _ok(**payload)
        finally:
            conn.close()
    except Exception as exc:
        _audit({
            "event": "cross_card_unblock_error",
            "root_task_id": root_id,
            "target_task_id": target_id,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
        })
        return _err(f"{type(exc).__name__}: {exc}"[:1000])

def register(ctx) -> None:
    ctx.register_tool(
        name="orchestration_unblock_card",
        toolset=TOOLSET,
        schema=SCHEMA,
        handler=_handle,
        check_fn=_available,
        emoji="🧭",
    )
