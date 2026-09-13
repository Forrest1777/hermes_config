"""Provider wait FSM and retry hardening for Hermes Kanban dispatch.

HERMES_PROVIDER_RECOVERY_2026_09_13

This plugin does not modify Hermes core. It observes completed/failed task
evidence, converts strong provider-availability failures into a durable
provider_wait state, and authorizes exactly one retry attempt after the
configured interval. Repeated provider failures can cycle indefinitely.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Any

MARKER = "HERMES_PROVIDER_RECOVERY_2026_09_13"
AUDIT = Path("/opt/data/logs/provider-recovery/events.jsonl")
DEFAULT_STATE = Path("/opt/data/logs/provider-recovery/state.json")
_LOCK = threading.Lock()

_PROVIDER_RE = re.compile(
    r"(?:"
    r"\b429\b|"
    r"rate[\s_-]*limit|"
    r"too many requests|"
    r"resource[\s_-]*exhausted|"
    r"provider[\s_-]*(?:unavailable|overloaded|error)|"
    r"temporar(?:ily|y)[\s_-]*unavailable|"
    r"server[\s_-]*overloaded|"
    r"(?:xai|openai|groq).{0,80}(?:429|rate[\s_-]*limit|overloaded)"
    r")",
    re.I | re.S,
)

_BLOCK_RE = re.compile(
    r"(?:blocked|needs_input|design_block|dependency|manual_intervention)",
    re.I,
)


def _cfg() -> dict[str, Any]:
    out: dict[str, Any] = {
        "retry_interval_seconds": 1200,
        "scan_limit": 300,
        "evidence_limit": 40,
        "allowed_profiles": [
            "implementation-orchestrator",
            "implementation-worker",
            "implementation-architect",
        ],
    }
    try:
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly() or {}).get("provider_recovery", {}) or {}
        if isinstance(raw, dict):
            for key in out:
                if key in raw:
                    out[key] = raw[key]
    except Exception:
        pass
    return out


def _state_path() -> Path:
    raw = str(os.environ.get("HERMES_PROVIDER_RECOVERY_STATE") or "").strip()
    return Path(raw) if raw else DEFAULT_STATE


def _audit(event: str, **payload: Any) -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"ts": int(time.time()), "event": event, **payload},
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ) + "\n")
    except Exception:
        pass


def _load_state() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _save_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    os.replace(tmp, path)


def _provider_signal(text: str) -> bool:
    return bool(_PROVIDER_RE.search(str(text or "")))


def _table_names(conn: Any) -> set[str]:
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        return {str(row[0]) for row in rows}
    except Exception:
        return set()


def _columns(conn: Any, table: str) -> set[str]:
    try:
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except Exception:
        return set()


def _row_dict(row: Any) -> dict[str, Any]:
    try:
        return {key: row[key] for key in row.keys()}
    except Exception:
        try:
            return dict(row)
        except Exception:
            return {"raw": str(row)}


def _row_text(row: Any) -> str:
    return json.dumps(_row_dict(row), ensure_ascii=False, sort_keys=True, default=str)


def _query_recent(conn: Any, table: str, task_id: str, limit: int) -> list[Any]:
    cols = _columns(conn, table)
    if "task_id" not in cols:
        return []
    order = "id DESC" if "id" in cols else (
        "created_at DESC" if "created_at" in cols else "rowid DESC"
    )
    try:
        return list(conn.execute(
            f"SELECT * FROM {table} WHERE task_id=? ORDER BY {order} LIMIT ?",
            (task_id, max(1, int(limit))),
        ).fetchall())
    except Exception:
        return []


def _fingerprint(source: str, row: Any) -> str:
    data = _row_dict(row)
    identity = (
        data.get("id")
        or data.get("run_id")
        or data.get("created_at")
        or data.get("started_at")
        or data.get("finished_at")
        or ""
    )
    raw = f"{source}|{identity}|{_row_text(row)}".encode("utf-8", "replace")
    return hashlib.sha256(raw).hexdigest()


def _latest_provider_evidence(conn: Any, task_id: str, limit: int) -> dict[str, Any] | None:
    tables = _table_names(conn)
    candidates: list[dict[str, Any]] = []
    for table in ("task_events", "task_runs"):
        if table not in tables:
            continue
        rows = _query_recent(conn, table, task_id, limit)
        for rank, row in enumerate(rows):
            text = _row_text(row)
            if not _provider_signal(text):
                continue
            data = _row_dict(row)
            candidates.append({
                "source": table,
                "rank": rank,
                "id": data.get("id"),
                "run_id": data.get("run_id"),
                "created_at": data.get("created_at") or data.get("finished_at"),
                "fingerprint": _fingerprint(table, row),
                "sample": text[-1800:],
            })
            break
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            int(item.get("id") or 0),
            int(item.get("created_at") or 0) if str(item.get("created_at") or "").isdigit() else 0,
            -int(item.get("rank") or 0),
        ),
        reverse=True,
    )
    return candidates[0]


def _task(conn: Any, task_id: str) -> dict[str, Any] | None:
    try:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    except Exception:
        return None
    return _row_dict(row) if row is not None else None


def _task_candidates(conn: Any, limit: int) -> list[dict[str, Any]]:
    cols = _columns(conn, "tasks")
    if not {"id", "status", "assignee"}.issubset(cols):
        return []
    clauses = ["status IN ('ready','blocked','triage')"]
    if "current_run_id" in cols:
        clauses.append("current_run_id IS NULL")
    sql = "SELECT * FROM tasks WHERE " + " AND ".join(clauses) + " LIMIT ?"
    try:
        return [_row_dict(r) for r in conn.execute(sql, (max(1, int(limit)),)).fetchall()]
    except Exception:
        return []


def _insert_status_event(conn: Any, task_id: str, payload: dict[str, Any], now: int) -> int | None:
    if "task_events" not in _table_names(conn):
        return None
    cols = _columns(conn, "task_events")
    names: list[str] = []
    values: list[Any] = []
    if "task_id" in cols:
        names.append("task_id"); values.append(task_id)
    else:
        return None
    if "run_id" in cols:
        names.append("run_id"); values.append(None)
    if "kind" in cols:
        names.append("kind"); values.append("status")
    if "payload" in cols:
        names.append("payload"); values.append(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    if "created_at" in cols:
        names.append("created_at"); values.append(now)
    placeholders = ",".join("?" for _ in names)
    cur = conn.execute(
        f"INSERT INTO task_events ({','.join(names)}) VALUES ({placeholders})",
        tuple(values),
    )
    try:
        return int(cur.lastrowid)
    except Exception:
        return None


def _write_txn(kb: Any, conn: Any):
    try:
        return kb.write_txn(conn)
    except Exception:
        return nullcontext()


def _clear_assignments(cols: set[str]) -> list[str]:
    out: list[str] = []
    for name in (
        "completed_at", "claim_lock", "claim_expires",
        "worker_pid", "current_run_id"
    ):
        if name in cols:
            out.append(f"{name}=NULL")
    return out


def _cas_status(conn: Any, task_id: str, from_statuses: tuple[str, ...], to_status: str) -> bool:
    cols = _columns(conn, "tasks")
    if not {"id", "status"}.issubset(cols):
        return False
    assignments = ["status=?"] + _clear_assignments(cols)
    placeholders = ",".join("?" for _ in from_statuses)
    where = [f"id=?", f"status IN ({placeholders})"]
    params: list[Any] = [to_status, task_id, *from_statuses]
    if "current_run_id" in cols:
        where.append("current_run_id IS NULL")
    cur = conn.execute(
        f"UPDATE tasks SET {','.join(assignments)} WHERE {' AND '.join(where)}",
        tuple(params),
    )
    return int(getattr(cur, "rowcount", 0) or 0) == 1


def _add_comment(kb: Any, conn: Any, task_id: str, body: str) -> None:
    try:
        kb.add_comment(conn, task_id, author="provider-recovery", body=body)
    except Exception:
        pass


def _newer_non_provider_block(conn: Any, task_id: str, wait_event_id: int | None) -> bool:
    if not wait_event_id:
        return False
    if "task_events" not in _table_names(conn):
        return False
    cols = _columns(conn, "task_events")
    if not {"id", "task_id"}.issubset(cols):
        return False
    try:
        rows = conn.execute(
            "SELECT * FROM task_events WHERE task_id=? AND id>? ORDER BY id ASC LIMIT 80",
            (task_id, int(wait_event_id)),
        ).fetchall()
    except Exception:
        return False
    for row in rows:
        text = _row_text(row)
        if _provider_signal(text):
            continue
        if _BLOCK_RE.search(text):
            return True
    return False


def _enter_wait(
    kb: Any,
    conn: Any,
    task_id: str,
    evidence: dict[str, Any],
    entry: dict[str, Any] | None,
    now: int,
    interval: int,
) -> dict[str, Any] | None:
    task = _task(conn, task_id)
    if not task:
        return None
    status = str(task.get("status") or "")
    if status not in {"ready", "blocked", "triage"}:
        return None
    with _write_txn(kb, conn):
        if not _cas_status(conn, task_id, ("ready", "blocked", "triage"), "blocked"):
            return None
        payload = {
            "status": "blocked",
            "kind": "provider_wait",
            "source": "provider-recovery",
            "retry_after_seconds": interval,
            "failure_fingerprint": evidence["fingerprint"],
        }
        wait_event_id = _insert_status_event(conn, task_id, payload, now)
    attempt = int((entry or {}).get("retry_epoch") or 0)
    new_entry = {
        "state": "provider_wait",
        "failure_fingerprint": evidence["fingerprint"],
        "evidence_source": evidence.get("source"),
        "evidence_id": evidence.get("id"),
        "wait_started_at": now,
        "next_retry_at": now + interval,
        "retry_epoch": attempt,
        "wait_event_id": wait_event_id,
    }
    _add_comment(
        kb, conn, task_id,
        "PROVIDER_WAIT_FSM\n"
        "state: PROVIDER_WAIT\n"
        f"retry_epoch: {attempt}\n"
        f"next_retry_at: {now + interval}\n"
        f"failure_fingerprint: {evidence['fingerprint']}\n"
        "policy: one authorization = one attempt; repeated provider failures return to PROVIDER_WAIT indefinitely.\n",
    )
    _audit(
        "provider_wait_entered",
        task_id=task_id,
        retry_epoch=attempt,
        next_retry_at=now + interval,
        evidence=evidence,
    )
    return new_entry


def _authorize_retry(
    kb: Any,
    conn: Any,
    task_id: str,
    entry: dict[str, Any],
    now: int,
) -> dict[str, Any]:
    task = _task(conn, task_id)
    if not task:
        return entry
    if str(task.get("status") or "") != "blocked":
        return entry
    if task.get("current_run_id") not in (None, ""):
        return entry
    if _newer_non_provider_block(conn, task_id, entry.get("wait_event_id")):
        _audit("provider_retry_held_by_newer_block", task_id=task_id)
        return entry

    changed = False
    try:
        changed = bool(kb.unblock_task(conn, task_id))
    except Exception:
        changed = False

    if not changed:
        with _write_txn(kb, conn):
            changed = _cas_status(conn, task_id, ("blocked",), "ready")
            if changed:
                _insert_status_event(conn, task_id, {
                    "status": "ready",
                    "from": "provider_wait",
                    "source": "provider-recovery",
                    "reason": "retry_interval_elapsed",
                }, now)

    if not changed:
        _audit("provider_retry_authorization_failed", task_id=task_id)
        return entry

    epoch = int(entry.get("retry_epoch") or 0) + 1
    new_entry = dict(entry)
    new_entry.update({
        "state": "retry_authorized",
        "retry_epoch": epoch,
        "authorized_at": now,
        "next_retry_at": None,
    })
    _add_comment(
        kb, conn, task_id,
        "PROVIDER_RETRY_AUTHORIZED\n"
        f"retry_epoch: {epoch}\n"
        "authorization_scope: exactly one dispatch attempt\n"
        "provider_wait_cleared: true\n",
    )
    _audit("provider_retry_authorized", task_id=task_id, retry_epoch=epoch)
    return new_entry


def _process_conn(kb: Any, conn: Any, now: int | None = None) -> dict[str, Any]:
    cfg = _cfg()
    now_i = int(now if now is not None else time.time())
    interval = max(60, int(cfg.get("retry_interval_seconds") or 1200))
    evidence_limit = max(5, int(cfg.get("evidence_limit") or 40))
    scan_limit = max(1, int(cfg.get("scan_limit") or 300))
    allowed = {str(x) for x in (cfg.get("allowed_profiles") or [])}

    state = _load_state()
    changed_state = False
    seen: set[str] = set()

    for task in _task_candidates(conn, scan_limit):
        task_id = str(task.get("id") or "")
        if not task_id:
            continue
        seen.add(task_id)
        if allowed and str(task.get("assignee") or "") not in allowed:
            continue
        entry = state.get(task_id)
        evidence = _latest_provider_evidence(conn, task_id, evidence_limit)

        if isinstance(entry, dict) and entry.get("state") == "provider_wait":
            if now_i >= int(entry.get("next_retry_at") or 0):
                updated = _authorize_retry(kb, conn, task_id, entry, now_i)
                if updated != entry:
                    state[task_id] = updated
                    changed_state = True
            continue

        if evidence is None:
            continue

        if isinstance(entry, dict) and evidence["fingerprint"] == entry.get("failure_fingerprint"):
            # The same failure can authorize only one retry attempt.
            continue

        updated = _enter_wait(
            kb, conn, task_id, evidence,
            entry if isinstance(entry, dict) else None,
            now_i, interval,
        )
        if updated is not None:
            state[task_id] = updated
            changed_state = True

    # Clean terminal states without deleting audit history.
    for task_id in list(state.keys()):
        if task_id in seen:
            continue
        task = _task(conn, task_id)
        if task and str(task.get("status") or "") in {"completed", "cancelled", "failed"}:
            state.pop(task_id, None)
            changed_state = True
            _audit("provider_state_terminal_cleanup", task_id=task_id, status=task.get("status"))

    if changed_state:
        _save_state(state)
    return state


def _tick(board=None, dry_run=False, **kwargs):
    del kwargs
    if dry_run:
        return None
    if not _LOCK.acquire(blocking=False):
        return True
    try:
        from hermes_cli import kanban_db as kb
        with kb.connect_closing(board=board) as conn:
            _process_conn(kb, conn)
        return True
    except Exception as exc:
        _audit("provider_recovery_tick_error", error=f"{type(exc).__name__}: {exc}"[:1200])
        return None
    finally:
        _LOCK.release()


def register(ctx) -> None:
    ctx.register_hook("on_kanban_dispatch_tick", _tick)