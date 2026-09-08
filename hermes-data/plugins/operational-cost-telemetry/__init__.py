"""Operational cost telemetry for Hermes Kanban workflows.

Read-only with respect to Kanban and project repositories. The plugin correlates
Kanban task runs with Hermes session telemetry already persisted in profile
state.db files. It never estimates missing token/cost data and never invokes an
LLM. Optional report persistence writes only under /opt/data/logs.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import time
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any, Iterable, Optional

NAME = "operational-cost-telemetry"
VERSION = "1.0.0"
TOOLSET = "operational_cost_telemetry"
ALLOWED_PROFILES = {"implementation-orchestrator"}
MARKER = "HERMES_OPERATIONAL_COST_TELEMETRY_2026_09_07"

REPORT_SCHEMA = {
    "name": "operational_cost_report",
    "description": (
        "Build a deterministic token/cost/runtime report for a Kanban card or root. "
        "Uses task_runs + metadata.worker_session_id + Hermes profile state.db sessions. "
        "No LLM call, Kanban mutation, project-repo write, pricing guess or missing-data estimation."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "Root/card task id. Defaults to HERMES_KANBAN_TASK in the current orchestrator run.",
            },
            "include_descendants": {
                "type": "boolean",
                "description": "Include transitive task_links descendants. Default true.",
            },
            "include_legacy_body_fallback": {
                "type": "boolean",
                "description": "Also recognize legacy parent_card_id/logical_parent_card_id body fields when no task_link is present. Default true.",
            },
            "detail_level": {
                "type": "string",
                "enum": ["summary", "cards"],
                "description": "summary returns compact totals; cards also returns per-card breakdown. Full JSON is persisted when enabled.",
            },
            "persist_report": {
                "type": "boolean",
                "description": "Persist full JSON under /opt/data/logs/operational-cost-telemetry/reports. Default true.",
            },
        },
        "additionalProperties": False,
    },
}


def _profile() -> str:
    return str(os.environ.get("HERMES_PROFILE") or "")


def _available() -> bool:
    return _profile() in ALLOWED_PROFILES


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False)


def _cfg() -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "profiles_root": "/opt/data/profiles",
        "report_dir": "/opt/data/logs/operational-cost-telemetry/reports",
        "max_cards": 1000,
        "max_sessions": 5000,
        "top_n": 10,
        "gwrm_events_path": "/opt/data/logs/gwrm-gut-runner/events.jsonl",
        "gwrm_cache_path": "/opt/data/logs/gwrm-gut-runner/terminal-cache-v2.json",
        "auto_report_on_session_end": True,
    }
    try:
        from hermes_cli.config import load_config

        raw = load_config().get("operational_cost_telemetry", {}) or {}
        if isinstance(raw, dict):
            for key in (
                "profiles_root", "report_dir", "max_cards", "max_sessions", "top_n",
                "gwrm_events_path", "gwrm_cache_path", "auto_report_on_session_end",
            ):
                if key in raw:
                    cfg[key] = raw[key]
    except Exception:
        pass
    return cfg


def _audit(payload: dict[str, Any], report_dir: Optional[Path] = None) -> None:
    try:
        root = (report_dir.parent if report_dir else Path("/opt/data/logs/operational-cost-telemetry"))
        root.mkdir(parents=True, exist_ok=True)
        path = root / "events.jsonl"
        record = {
            "ts": int(time.time()),
            "plugin": NAME,
            "plugin_version": VERSION,
            "profile": _profile(),
            **payload,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f'PRAGMA table_info("{table}")')}
    except Exception:
        return set()


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return bool(row)


def _rows_as_dicts(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    cols = [d[0] for d in cursor.description or []]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def _safe_json(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(str(raw))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _legacy_parent_from_body(body: Any) -> Optional[str]:
    text = str(body or "")
    # Prefer explicit logical_parent_card_id over generic parent_card_id.
    for key in ("logical_parent_card_id", "parent_card_id"):
        m = re.search(rf"(?mi)^\s*{re.escape(key)}\s*:\s*(t_[A-Za-z0-9]+)\s*$", text)
        if m:
            return m.group(1)
    return None


def _root_phase_hint(conn: sqlite3.Connection, task_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {"phase_id": None, "checkpoint_version": None}
    if not _table_exists(conn, "task_comments"):
        return out
    cols = _table_columns(conn, "task_comments")
    if not {"task_id", "body"}.issubset(cols):
        return out
    order = "id DESC" if "id" in cols else "rowid DESC"
    rows = conn.execute(
        f"SELECT body FROM task_comments WHERE task_id=? ORDER BY {order} LIMIT 100", (task_id,)
    ).fetchall()
    for (body,) in rows:
        text = str(body or "")
        if "OPERATIONAL_CHECKPOINT_CANONICAL" not in text:
            continue
        mv = re.search(r"OPERATIONAL_CHECKPOINT_CANONICAL\s+v(\d+)", text)
        mp = re.search(r"(?mi)^\s*phase_id\s*:\s*([^\r\n#]+)", text)
        out["checkpoint_version"] = int(mv.group(1)) if mv else None
        out["phase_id"] = mp.group(1).strip().strip("'\"") if mp else None
        return out
    return out


def _load_tasks(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    cols = _table_columns(conn, "tasks")
    wanted = [
        c
        for c in (
            "id",
            "title",
            "status",
            "assignee",
            "body",
            "project_id",
            "created_at",
            "session_id",
        )
        if c in cols
    ]
    if "id" not in wanted:
        raise RuntimeError("Kanban tasks table lacks id")
    cur = conn.execute("SELECT " + ", ".join(wanted) + " FROM tasks")
    return {str(r["id"]): r for r in _rows_as_dicts(cur)}


def _scope_tasks(
    conn: sqlite3.Connection,
    root_task_id: str,
    *,
    include_descendants: bool,
    include_legacy_body_fallback: bool,
    max_cards: int,
) -> tuple[list[str], dict[str, str], list[dict[str, Any]]]:
    tasks = _load_tasks(conn)
    if root_task_id not in tasks:
        raise RuntimeError(f"task not found: {root_task_id}")

    relation_source: dict[str, str] = {root_task_id: "root"}
    conflicts: list[dict[str, Any]] = []
    if not include_descendants:
        return [root_task_id], relation_source, conflicts

    root_project = tasks[root_task_id].get("project_id")
    adjacency: dict[str, list[str]] = defaultdict(list)
    if _table_exists(conn, "task_links"):
        cols = _table_columns(conn, "task_links")
        if {"parent_id", "child_id"}.issubset(cols):
            for parent, child in conn.execute("SELECT parent_id, child_id FROM task_links"):
                parent, child = str(parent), str(child)
                if parent in tasks and child in tasks:
                    if root_project is not None and tasks[child].get("project_id") not in (None, root_project):
                        continue
                    adjacency[parent].append(child)

    scoped: set[str] = {root_task_id}
    q: deque[str] = deque([root_task_id])
    while q:
        parent = q.popleft()
        for child in adjacency.get(parent, []):
            if child not in scoped:
                scoped.add(child)
                relation_source[child] = "task_links"
                q.append(child)
                if len(scoped) > max_cards:
                    raise RuntimeError(f"scope exceeds max_cards={max_cards}")

    if include_legacy_body_fallback:
        # Only add legacy edges when the task is not already structurally linked.
        # Iterate to allow chains of legacy parent references.
        changed = True
        while changed:
            changed = False
            for tid, task in tasks.items():
                if tid in scoped:
                    continue
                if root_project is not None and task.get("project_id") not in (None, root_project):
                    continue
                legacy_parent = _legacy_parent_from_body(task.get("body"))
                if not legacy_parent or legacy_parent not in scoped:
                    continue
                # If task_links already gives this child another parent, do not infer.
                structural_parents = [p for p, children in adjacency.items() if tid in children]
                if structural_parents and legacy_parent not in structural_parents:
                    conflicts.append({
                        "task_id": tid,
                        "legacy_parent": legacy_parent,
                        "task_link_parents": sorted(structural_parents),
                    })
                    continue
                scoped.add(tid)
                relation_source[tid] = "legacy_body"
                changed = True
                if len(scoped) > max_cards:
                    raise RuntimeError(f"scope exceeds max_cards={max_cards}")

    # Stable order: root first, then creation time/id.
    rest = sorted(
        (tid for tid in scoped if tid != root_task_id),
        key=lambda tid: (tasks[tid].get("created_at") or 0, tid),
    )
    return [root_task_id, *rest], relation_source, conflicts


def _load_runs(conn: sqlite3.Connection, task_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = list(task_ids)
    if not ids or not _table_exists(conn, "task_runs"):
        return []
    cols = _table_columns(conn, "task_runs")
    wanted = [
        c
        for c in (
            "id",
            "task_id",
            "profile",
            "status",
            "started_at",
            "ended_at",
            "outcome",
            "metadata",
            "error",
        )
        if c in cols
    ]
    placeholders = ",".join("?" for _ in ids)
    cur = conn.execute(
        "SELECT " + ", ".join(wanted) + f" FROM task_runs WHERE task_id IN ({placeholders}) ORDER BY task_id, started_at, id",
        tuple(ids),
    )
    rows = _rows_as_dicts(cur)
    for r in rows:
        meta = _safe_json(r.get("metadata"))
        r["metadata_parsed"] = meta
        r["worker_session_id"] = str(meta.get("worker_session_id") or "").strip() or None
    return rows



def _reason_class(run: dict[str, Any]) -> tuple[str, str | None]:
    meta = run.get("metadata_parsed") if isinstance(run.get("metadata_parsed"), dict) else {}
    retry_status = str(meta.get("retry_status") or "").strip()
    error = str(run.get("error") or "").strip()
    summary = str(run.get("summary") or "").strip()
    status = str(run.get("status") or run.get("outcome") or "unknown").strip().lower()

    if retry_status:
        return f"retry_status:{retry_status}", error or summary or None
    if "protocol violation" in error.lower():
        return "protocol_violation", error
    if summary:
        m = re.match(r"^([A-Z][A-Z0-9_ -]{2,64})(?::|\s+-\s+)", summary)
        if m:
            label = re.sub(r"[ -]+", "_", m.group(1).strip()).lower()
            return label, summary
        upper = summary.upper()
        for token in (
            "BLOCKED_OPERATIONAL", "BLOCKED_BY_DESIGN", "PROVIDER_WAIT",
            "RETRY_CHECKPOINT_GUARD", "VALIDATION_FAILED", "READY_FOR_INTEGRATION",
        ):
            if token in upper:
                return token.lower(), summary
    return status or "unknown", error or summary or None


def _load_task_events(conn: sqlite3.Connection, task_ids: Iterable[str]) -> list[dict[str, Any]]:
    ids = list(task_ids)
    if not ids or not _table_exists(conn, "task_events"):
        return []
    cols = _table_columns(conn, "task_events")
    wanted = [c for c in ("id", "task_id", "run_id", "kind", "payload", "created_at") if c in cols]
    if not {"task_id", "kind"}.issubset(cols):
        return []
    ph = ",".join("?" for _ in ids)
    cur = conn.execute(
        "SELECT " + ", ".join(wanted) + f" FROM task_events WHERE task_id IN ({ph}) ORDER BY created_at, id",
        tuple(ids),
    )
    rows = _rows_as_dicts(cur)
    for row in rows:
        row["payload_parsed"] = _safe_json(row.get("payload"))
    return rows


def _structured_test_records(runs: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for run in runs:
        meta = run.get("metadata_parsed") if isinstance(run.get("metadata_parsed"), dict) else {}
        tests = meta.get("tests")
        if not isinstance(tests, list):
            continue
        for item in tests:
            if not isinstance(item, dict):
                continue
            op = str(item.get("operation_id") or "").strip()
            if not op:
                continue
            evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
            counts = {
                "scripts": item.get("scripts", evidence.get("scripts")),
                "tests": item.get("tests", evidence.get("tests")),
                "asserts": item.get("asserts", evidence.get("asserts")),
                "failing_tests": item.get("failures", evidence.get("failures")),
                "errors": item.get("errors", evidence.get("errors")),
            }
            out[op] = {
                "operation_id": op,
                "task_id": str(run.get("task_id") or ""),
                "run_id": run.get("id"),
                "selection": item.get("command"),
                "result": item.get("result"),
                "duration_ms": item.get("duration_ms", evidence.get("duration_ms")),
                "counts": counts,
                "source": "task_run_metadata",
            }
    return out


def _load_gwrm_audit(path: Path, scoped_task_ids: set[str]) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if not isinstance(row, dict):
                    continue
                tid = str(row.get("task_id") or "")
                if tid and tid in scoped_task_ids:
                    rows.append(row)
    except Exception:
        return []
    return rows


def _load_gwrm_cache_by_operation(path: Path) -> dict[str, dict[str, Any]]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    if not isinstance(raw, dict):
        return out
    for entry in raw.values():
        if not isinstance(entry, dict):
            continue
        payload = entry.get("payload")
        if not isinstance(payload, dict):
            continue
        op = str(payload.get("operation_id") or "").strip()
        if op:
            out[op] = payload
    return out


def _build_gut_telemetry(
    runs: list[dict[str, Any]],
    task_ids: list[str],
    *,
    events_path: Path,
    cache_path: Path,
    top_n: int,
) -> dict[str, Any]:
    structured = _structured_test_records(runs)
    audit = _load_gwrm_audit(events_path, set(task_ids))
    cached = _load_gwrm_cache_by_operation(cache_path)
    ops: dict[str, dict[str, Any]] = {}

    def ensure(op: str) -> dict[str, Any]:
        return ops.setdefault(op, {
            "operation_id": op,
            "task_ids": set(),
            "run_ids": set(),
            "selection": None,
            "status": None,
            "passed": None,
            "counts": {"scripts": None, "tests": None, "asserts": None, "failing_tests": None, "errors": None},
            "duration_ms": None,
            "duration_source": None,
            "first_seen_ts": None,
            "terminal_ts": None,
            "observed_wall_ms": None,
            "started_reuse_hits": 0,
            "terminal_cache_hits": 0,
            "wait_existing_calls": 0,
            "wait_window_expirations": 0,
            "sources": set(),
        })

    for op, rec in structured.items():
        d = ensure(op)
        d["task_ids"].add(rec.get("task_id"))
        if rec.get("run_id") is not None:
            d["run_ids"].add(rec.get("run_id"))
        d["selection"] = rec.get("selection") or d["selection"]
        d["status"] = rec.get("result") or d["status"]
        if str(rec.get("result") or "").lower() in ("pass", "passed", "true", "success"):
            d["passed"] = True
        elif str(rec.get("result") or "").lower() in ("fail", "failed", "false", "failure"):
            d["passed"] = False
        for key, val in (rec.get("counts") or {}).items():
            if val is not None:
                d["counts"][key] = val
        if rec.get("duration_ms") is not None:
            d["duration_ms"] = rec.get("duration_ms")
            d["duration_source"] = "task_run_metadata"
        d["sources"].add("task_run_metadata")

    for row in audit:
        op = str(row.get("operation_id") or "").strip()
        if not op:
            continue
        d = ensure(op)
        tid = str(row.get("task_id") or "").strip()
        if tid:
            d["task_ids"].add(tid)
        ts = _f(row.get("ts"))
        if ts is not None:
            if d["first_seen_ts"] is None or ts < d["first_seen_ts"]:
                d["first_seen_ts"] = ts
        ev = str(row.get("event") or "")
        if row.get("selection") and not d["selection"]:
            d["selection"] = row.get("selection")
        if ev == "started" and bool(row.get("reused_existing_operation")):
            d["started_reuse_hits"] += 1
        elif ev == "terminal_cache_hit":
            d["terminal_cache_hits"] += 1
        elif ev == "wait_existing_started":
            d["wait_existing_calls"] += 1
        elif ev == "wait_window_expired":
            d["wait_window_expirations"] += 1
        elif ev == "terminal":
            d["status"] = row.get("status") or d["status"]
            if row.get("passed") is not None:
                d["passed"] = bool(row.get("passed"))
            if ts is not None:
                d["terminal_ts"] = ts
        d["sources"].add("gwrm_gut_runner_audit")

    for op, payload in cached.items():
        if op not in ops:
            continue
        d = ensure(op)
        counts = payload.get("counts") if isinstance(payload.get("counts"), dict) else {}
        for key in ("scripts", "tests", "asserts", "failing_tests", "errors"):
            if d["counts"].get(key) is None and counts.get(key) is not None:
                d["counts"][key] = counts.get(key)
        if d["duration_ms"] is None and payload.get("duration_ms") is not None:
            d["duration_ms"] = payload.get("duration_ms")
            d["duration_source"] = "gwrm_terminal_cache"
        if d["passed"] is None and payload.get("passed") is not None:
            d["passed"] = bool(payload.get("passed"))
        if not d["selection"]:
            sel = payload.get("selection")
            if isinstance(sel, dict):
                typ, val = sel.get("type"), sel.get("value")
                d["selection"] = f"{typ}:{val}" if typ and val else None
            elif sel:
                d["selection"] = str(sel)
        d["sources"].add("gwrm_terminal_cache")

    operation_rows: list[dict[str, Any]] = []
    for op in sorted(ops):
        d = ops[op]
        if d["first_seen_ts"] is not None and d["terminal_ts"] is not None and d["terminal_ts"] >= d["first_seen_ts"]:
            d["observed_wall_ms"] = int((d["terminal_ts"] - d["first_seen_ts"]) * 1000)
        d["task_ids"] = sorted(x for x in d["task_ids"] if x)
        d["run_ids"] = sorted(d["run_ids"])
        d["sources"] = sorted(d["sources"])
        operation_rows.append(d)

    by_sel: dict[str, dict[str, Any]] = {}
    for op in operation_rows:
        sel = str(op.get("selection") or "unknown")
        d = by_sel.setdefault(sel, {
            "selection": sel,
            "operation_count": 0,
            "repeat_operation_count": 0,
            "terminal_cache_hits": 0,
            "started_reuse_hits": 0,
            "wait_existing_calls": 0,
            "wait_window_expirations": 0,
            "known_duration_ms": 0,
            "observed_wall_ms": 0,
            "tests": 0,
            "asserts": 0,
            "task_ids": set(),
        })
        d["operation_count"] += 1
        d["terminal_cache_hits"] += _n(op.get("terminal_cache_hits"))
        d["started_reuse_hits"] += _n(op.get("started_reuse_hits"))
        d["wait_existing_calls"] += _n(op.get("wait_existing_calls"))
        d["wait_window_expirations"] += _n(op.get("wait_window_expirations"))
        d["known_duration_ms"] += _n(op.get("duration_ms"))
        d["observed_wall_ms"] += _n(op.get("observed_wall_ms"))
        d["tests"] += _n((op.get("counts") or {}).get("tests"))
        d["asserts"] += _n((op.get("counts") or {}).get("asserts"))
        d["task_ids"].update(op.get("task_ids") or [])
    repeated = []
    for d in by_sel.values():
        d["repeat_operation_count"] = max(0, d["operation_count"] - 1)
        d["task_ids"] = sorted(d["task_ids"])
        repeated.append(d)
    repeated.sort(
        key=lambda x: (
            x["repeat_operation_count"], x["operation_count"], x["wait_existing_calls"],
            x["terminal_cache_hits"], x["observed_wall_ms"],
        ),
        reverse=True,
    )

    counts_coverage = sum(1 for o in operation_rows if (o.get("counts") or {}).get("tests") is not None)
    duration_coverage = sum(1 for o in operation_rows if o.get("duration_ms") is not None)
    return {
        "operation_count": len(operation_rows),
        "passed_operations": sum(1 for o in operation_rows if o.get("passed") is True),
        "failed_operations": sum(1 for o in operation_rows if o.get("passed") is False),
        "counts_coverage_operations": counts_coverage,
        "duration_coverage_operations": duration_coverage,
        "total_known_duration_ms": sum(_n(o.get("duration_ms")) for o in operation_rows),
        "total_observed_wall_ms": sum(_n(o.get("observed_wall_ms")) for o in operation_rows),
        "total_scripts": sum(_n((o.get("counts") or {}).get("scripts")) for o in operation_rows),
        "total_tests": sum(_n((o.get("counts") or {}).get("tests")) for o in operation_rows),
        "total_asserts": sum(_n((o.get("counts") or {}).get("asserts")) for o in operation_rows),
        "reuse": {
            "terminal_cache_hits": sum(_n(o.get("terminal_cache_hits")) for o in operation_rows),
            "started_reuse_hits": sum(_n(o.get("started_reuse_hits")) for o in operation_rows),
            "wait_existing_calls": sum(_n(o.get("wait_existing_calls")) for o in operation_rows),
            "wait_window_expirations": sum(_n(o.get("wait_window_expirations")) for o in operation_rows),
        },
        "operations": operation_rows,
        "top_repeated_operations": repeated[:max(1, top_n)],
        "sources": {
            "gwrm_events_path": str(events_path),
            "gwrm_events_available": events_path.is_file(),
            "gwrm_cache_path": str(cache_path),
            "gwrm_cache_available": cache_path.is_file(),
        },
    }


def _query_sessions_for_ids(profiles_root: Path, session_ids: set[str], max_sessions: int) -> dict[str, list[dict[str, Any]]]:
    if len(session_ids) > max_sessions:
        raise RuntimeError(f"session scope exceeds max_sessions={max_sessions}")
    matches: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if not session_ids:
        return matches

    for db_path in sorted(profiles_root.glob("*/state.db")):
        try:
            db = sqlite3.connect(str(db_path))
            db.row_factory = sqlite3.Row
            if not _table_exists(db, "sessions"):
                db.close()
                continue
            cols = _table_columns(db, "sessions")
            wanted = [
                c
                for c in (
                    "id",
                    "profile_name",
                    "model",
                    "started_at",
                    "ended_at",
                    "input_tokens",
                    "output_tokens",
                    "cache_read_tokens",
                    "cache_write_tokens",
                    "reasoning_tokens",
                    "api_call_count",
                    "tool_call_count",
                    "estimated_cost_usd",
                    "actual_cost_usd",
                    "cost_status",
                    "cost_source",
                    "billing_provider",
                )
                if c in cols
            ]
            ids = sorted(session_ids)
            # SQLite variable limits vary. Chunk conservatively.
            for i in range(0, len(ids), 400):
                chunk = ids[i : i + 400]
                ph = ",".join("?" for _ in chunk)
                cur = db.execute(
                    "SELECT " + ", ".join(wanted) + f" FROM sessions WHERE id IN ({ph})",
                    tuple(chunk),
                )
                for row in cur.fetchall():
                    d = dict(row)
                    d["state_db_profile"] = db_path.parent.name
                    matches[str(d["id"])].append(d)
            db.close()
        except sqlite3.Error:
            try:
                db.close()
            except Exception:
                pass
            continue
    return matches


def _n(v: Any) -> int:
    try:
        return int(v or 0)
    except Exception:
        return 0


def _f(v: Any) -> Optional[float]:
    if v is None:
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _session_metrics(session: dict[str, Any]) -> dict[str, Any]:
    inp = _n(session.get("input_tokens"))
    out = _n(session.get("output_tokens"))
    cr = _n(session.get("cache_read_tokens"))
    cw = _n(session.get("cache_write_tokens"))
    rs = _n(session.get("reasoning_tokens"))
    status = str(session.get("cost_status") or "unknown").lower()
    actual = _f(session.get("actual_cost_usd"))
    estimated = _f(session.get("estimated_cost_usd"))
    known_cost: Optional[float] = None
    cost_kind: Optional[str] = None
    if actual is not None:
        known_cost, cost_kind = actual, "actual"
    elif estimated is not None and status not in ("", "unknown", "unavailable", "none"):
        known_cost, cost_kind = estimated, "estimated"
    return {
        "input_tokens": inp,
        "output_tokens": out,
        "cache_read_tokens": cr,
        "cache_write_tokens": cw,
        "reasoning_tokens": rs,
        "context_tokens": inp + cr + cw,
        "generation_tokens": out + rs,
        "observed_token_volume": inp + out + cr + cw + rs,
        "api_call_count": _n(session.get("api_call_count")),
        "tool_call_count": _n(session.get("tool_call_count")),
        "known_cost_usd": known_cost,
        "cost_kind": cost_kind,
        "cost_status": session.get("cost_status"),
        "model": session.get("model"),
        "profile_name": session.get("profile_name") or session.get("state_db_profile"),
        "billing_provider": session.get("billing_provider"),
    }


def _empty_metrics() -> dict[str, Any]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "reasoning_tokens": 0,
        "context_tokens": 0,
        "generation_tokens": 0,
        "observed_token_volume": 0,
        "api_call_count": 0,
        "tool_call_count": 0,
        "known_cost_usd": 0.0,
        "known_cost_sessions": 0,
        "unknown_cost_sessions": 0,
    }


def _add_metrics(dst: dict[str, Any], sm: dict[str, Any]) -> None:
    for key in (
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "context_tokens",
        "generation_tokens",
        "observed_token_volume",
        "api_call_count",
        "tool_call_count",
    ):
        dst[key] += _n(sm.get(key))
    if sm.get("known_cost_usd") is None:
        dst["unknown_cost_sessions"] += 1
    else:
        dst["known_cost_usd"] += float(sm["known_cost_usd"])
        dst["known_cost_sessions"] += 1


def _round_cost(v: float) -> float:
    return round(float(v), 8)


def _build_report(
    conn: sqlite3.Connection,
    *,
    root_task_id: str,
    profiles_root: Path,
    include_descendants: bool = True,
    include_legacy_body_fallback: bool = True,
    max_cards: int = 1000,
    max_sessions: int = 5000,
    top_n: int = 10,
    gwrm_events_path: Optional[Path] = None,
    gwrm_cache_path: Optional[Path] = None,
    now: Optional[float] = None,
) -> dict[str, Any]:
    now = float(now if now is not None else time.time())
    top_n = max(1, int(top_n))
    tasks = _load_tasks(conn)
    task_ids, relation_source, relation_conflicts = _scope_tasks(
        conn,
        root_task_id,
        include_descendants=include_descendants,
        include_legacy_body_fallback=include_legacy_body_fallback,
        max_cards=max_cards,
    )
    runs = _load_runs(conn, task_ids)
    task_events = _load_task_events(conn, task_ids)
    session_ids = {str(r["worker_session_id"]) for r in runs if r.get("worker_session_id")}
    matches = _query_sessions_for_ids(profiles_root, session_ids, max_sessions)

    session_resolution: dict[str, Optional[dict[str, Any]]] = {}
    ambiguous_session_ids: list[str] = []
    for sid in sorted(session_ids):
        found = matches.get(sid, [])
        if len(found) == 1:
            session_resolution[sid] = found[0]
        elif len(found) > 1:
            session_resolution[sid] = None
            ambiguous_session_ids.append(sid)
        else:
            session_resolution[sid] = None

    runs_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in runs:
        runs_by_task[str(r.get("task_id"))].append(r)
    events_by_task: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ev in task_events:
        events_by_task[str(ev.get("task_id"))].append(ev)

    # Unique-session totals avoid double counting if a session id appears on >1 run.
    aggregate = _empty_metrics()
    profiles = Counter()
    models = Counter()
    providers = Counter()
    resolved_unique_sessions: set[str] = set()
    missing_session_ids: set[str] = set()
    for sid in sorted(session_ids):
        session = session_resolution.get(sid)
        if not session:
            if sid not in ambiguous_session_ids:
                missing_session_ids.add(sid)
            continue
        resolved_unique_sessions.add(sid)
        sm = _session_metrics(session)
        _add_metrics(aggregate, sm)
        profiles[str(sm.get("profile_name") or "unknown")] += 1
        models[str(sm.get("model") or "unknown")] += 1
        providers[str(sm.get("billing_provider") or "unknown")] += 1

    outcomes = Counter(str(r.get("outcome") or r.get("status") or "unknown") for r in runs)
    run_statuses = Counter(str(r.get("status") or "unknown") for r in runs)
    run_count = len(runs)
    runs_with_sid = sum(1 for r in runs if r.get("worker_session_id"))
    runs_correlated = sum(
        1
        for r in runs
        if r.get("worker_session_id") and session_resolution.get(str(r["worker_session_id"])) is not None
    )
    open_runs = sum(1 for r in runs if r.get("ended_at") is None)

    durations: list[float] = []
    starts: list[float] = []
    ends: list[float] = []
    for r in runs:
        s, e = _f(r.get("started_at")), _f(r.get("ended_at"))
        if s is not None:
            starts.append(s)
        if e is not None:
            ends.append(e)
        if s is not None and e is not None and e >= s:
            durations.append(e - s)
    worker_runtime_seconds = round(sum(durations), 3)
    wall_clock_span_seconds = round(max(ends) - min(starts), 3) if starts and ends and max(ends) >= min(starts) else None

    # Per-run persistence including reason/resumption classification and correlated usage.
    run_rows: list[dict[str, Any]] = []
    retry_session_ids: set[str] = set()
    resumption_kinds = Counter()
    reason_counts = Counter()
    run_to_gut_ops: dict[int, list[str]] = defaultdict(list)
    structured_tests = _structured_test_records(runs)
    for op, rec in structured_tests.items():
        if rec.get("run_id") is not None:
            run_to_gut_ops[_n(rec.get("run_id"))].append(op)

    for tid in task_ids:
        trs = sorted(
            runs_by_task.get(tid, []),
            key=lambda r: (_f(r.get("started_at")) or 0, _n(r.get("id"))),
        )
        previous: Optional[dict[str, Any]] = None
        for index, r in enumerate(trs):
            sid = str(r.get("worker_session_id") or "").strip() or None
            sess = session_resolution.get(sid) if sid else None
            if not sid:
                corr = "missing_worker_session_id"
            elif sid in ambiguous_session_ids:
                corr = "ambiguous_session_id"
            elif sess is None:
                corr = "session_not_found"
            else:
                corr = "correlated"
            sm = _session_metrics(sess) if sess else None
            reason_class, reason_text = _reason_class(r)
            is_resumption = index > 0
            resumption_kind = None
            if is_resumption:
                prev_outcome = str((previous or {}).get("outcome") or (previous or {}).get("status") or "unknown").lower()
                if prev_outcome == "blocked":
                    resumption_kind = "after_block"
                elif prev_outcome == "crashed":
                    resumption_kind = "retry_after_crash"
                elif prev_outcome in ("timed_out", "timeout"):
                    resumption_kind = "retry_after_timeout"
                else:
                    resumption_kind = "additional_run"
                resumption_kinds[resumption_kind] += 1
                if sid and sess is not None:
                    retry_session_ids.add(sid)
            if str(r.get("status") or "").lower() not in ("done", "completed") or is_resumption:
                reason_counts[reason_class] += 1

            s, e = _f(r.get("started_at")), _f(r.get("ended_at"))
            duration = round(e - s, 3) if s is not None and e is not None and e >= s else None
            run_rows.append({
                "run_id": r.get("id"),
                "task_id": tid,
                "run_profile": r.get("profile"),
                "status": r.get("status"),
                "outcome": r.get("outcome"),
                "started_at": s,
                "ended_at": e,
                "duration_seconds": duration,
                "is_resumption": is_resumption,
                "resumption_kind": resumption_kind,
                "reason_class": reason_class,
                "reason": (reason_text[:1200] if reason_text else None),
                "worker_session_id": sid,
                "session_correlation": corr,
                "session_profile": sm.get("profile_name") if sm else None,
                "model": sm.get("model") if sm else None,
                "metrics": sm,
                "gut_operation_ids": sorted(run_to_gut_ops.get(_n(r.get("id")), [])),
            })
            previous = r

    extra_attempts = sum(max(0, len(v) - 1) for v in runs_by_task.values())
    retry_metrics = _empty_metrics()
    for sid in sorted(retry_session_ids):
        sess = session_resolution.get(sid)
        if sess:
            _add_metrics(retry_metrics, _session_metrics(sess))

    event_kind_counts = Counter(str(ev.get("kind") or "unknown") for ev in task_events)
    operational_event_interest = {
        key: event_kind_counts.get(key, 0)
        for key in (
            "dependency_wait",
            "governance_recovery_requeued",
            "respawn_guarded",
            "protocol_violation",
            "block_loop_detected",
            "blocked",
            "crashed",
            "promoted",
            "unblocked",
        )
        if event_kind_counts.get(key, 0)
    }

    card_rows: list[dict[str, Any]] = []
    for tid in task_ids:
        task = tasks[tid]
        trs = runs_by_task.get(tid, [])
        tids = {str(r["worker_session_id"]) for r in trs if r.get("worker_session_id")}
        tm = _empty_metrics()
        correlated = 0
        unavailable = 0
        for sid in sorted(tids):
            sess = session_resolution.get(sid)
            if sess is None:
                unavailable += 1
                continue
            correlated += 1
            _add_metrics(tm, _session_metrics(sess))
        tr_durations = []
        card_reasons = Counter()
        for r in trs:
            s, e = _f(r.get("started_at")), _f(r.get("ended_at"))
            if s is not None and e is not None and e >= s:
                tr_durations.append(e - s)
            rc, _ = _reason_class(r)
            if str(r.get("status") or "").lower() not in ("done", "completed"):
                card_reasons[rc] += 1
        card_event_counts = Counter(str(ev.get("kind") or "unknown") for ev in events_by_task.get(tid, []))
        card_gut_ids = sorted({
            op
            for r in trs
            for op in run_to_gut_ops.get(_n(r.get("id")), [])
        })
        card_rows.append({
            "task_id": tid,
            "title": task.get("title"),
            "status": task.get("status"),
            "assignee": task.get("assignee"),
            "relation_source": relation_source.get(tid, "unknown"),
            "run_count": len(trs),
            "resumption_count": max(0, len(trs) - 1),
            "outcomes": dict(Counter(str(r.get("outcome") or r.get("status") or "unknown") for r in trs)),
            "reason_counts": dict(card_reasons),
            "event_counts": dict(card_event_counts),
            "worker_runtime_seconds": round(sum(tr_durations), 3),
            "session_ids": sorted(tids),
            "correlated_sessions": correlated,
            "unavailable_sessions": unavailable,
            "gut_operation_ids": card_gut_ids,
            "metrics": {**tm, "known_cost_usd": _round_cost(tm["known_cost_usd"])},
        })

    gwrm_events_path = gwrm_events_path or Path("/opt/data/logs/gwrm-gut-runner/events.jsonl")
    gwrm_cache_path = gwrm_cache_path or Path("/opt/data/logs/gwrm-gut-runner/terminal-cache-v2.json")
    gut = _build_gut_telemetry(
        runs,
        task_ids,
        events_path=gwrm_events_path,
        cache_path=gwrm_cache_path,
        top_n=top_n,
    )

    # Enrich cards with GUT operations discovered from GWRM audit/cache even when handoff metadata omitted them.
    gut_by_task: dict[str, set[str]] = defaultdict(set)
    for op in gut["operations"]:
        for tid in op.get("task_ids") or []:
            gut_by_task[str(tid)].add(str(op.get("operation_id")))
    for card in card_rows:
        merged = set(card.get("gut_operation_ids") or []) | gut_by_task.get(str(card["task_id"]), set())
        card["gut_operation_ids"] = sorted(merged)

    phase_hint = _root_phase_hint(conn, root_task_id)
    task_statuses = Counter(str(tasks[tid].get("status") or "unknown") for tid in task_ids)
    coverage_pct = round((runs_correlated / run_count) * 100.0, 2) if run_count else 100.0
    usd_sessions = aggregate["known_cost_sessions"] + aggregate["unknown_cost_sessions"]
    usd_coverage_pct = round((aggregate["known_cost_sessions"] / usd_sessions) * 100.0, 2) if usd_sessions else 100.0

    aggregate["known_cost_usd"] = _round_cost(aggregate["known_cost_usd"])
    retry_metrics["known_cost_usd"] = _round_cost(retry_metrics["known_cost_usd"])

    top_token_consumers = sorted(
        (
            {
                "task_id": c["task_id"],
                "title": c.get("title"),
                "assignee": c.get("assignee"),
                "observed_token_volume": c["metrics"]["observed_token_volume"],
                "input_tokens": c["metrics"]["input_tokens"],
                "output_tokens": c["metrics"]["output_tokens"],
                "cache_read_tokens": c["metrics"]["cache_read_tokens"],
                "reasoning_tokens": c["metrics"]["reasoning_tokens"],
                "run_count": c["run_count"],
                "resumption_count": c["resumption_count"],
                "worker_runtime_seconds": c["worker_runtime_seconds"],
            }
            for c in card_rows
        ),
        key=lambda x: (x["observed_token_volume"], x["run_count"], x["worker_runtime_seconds"]),
        reverse=True,
    )[:top_n]
    top_resumed_cards = sorted(
        (
            {
                "task_id": c["task_id"],
                "title": c.get("title"),
                "resumption_count": c["resumption_count"],
                "run_count": c["run_count"],
                "observed_token_volume": c["metrics"]["observed_token_volume"],
                "worker_runtime_seconds": c["worker_runtime_seconds"],
            }
            for c in card_rows
            if c["resumption_count"] > 0
        ),
        key=lambda x: (x["resumption_count"], x["observed_token_volume"]),
        reverse=True,
    )[:top_n]

    report = {
        "schema_version": 1,
        "plugin": NAME,
        "plugin_version": VERSION,
        "generated_at_epoch": int(now),
        "scope": {
            "root_task_id": root_task_id,
            "phase_id": phase_hint.get("phase_id"),
            "checkpoint_version": phase_hint.get("checkpoint_version"),
            "include_descendants": include_descendants,
            "include_legacy_body_fallback": include_legacy_body_fallback,
            "task_count": len(task_ids),
            "task_ids": task_ids,
            "relation_sources": dict(Counter(relation_source.get(tid, "unknown") for tid in task_ids)),
            "relation_conflicts": relation_conflicts,
        },
        "finality": {
            "status": (
                "final"
                if str(tasks[root_task_id].get("status") or "").lower() in {"done", "archived"} and open_runs == 0
                else "provisional"
            ),
            "root_task_status": str(tasks[root_task_id].get("status") or "unknown"),
            "open_runs": open_runs,
        },
        "operations": {
            "task_statuses": dict(task_statuses),
            "run_count": run_count,
            "run_statuses": dict(run_statuses),
            "outcomes": dict(outcomes),
            "resumption_count": extra_attempts,
            "extra_attempts": extra_attempts,
            "resumption_kinds": dict(resumption_kinds),
            "open_runs": open_runs,
            "worker_runtime_seconds": worker_runtime_seconds,
            "wall_clock_span_seconds": wall_clock_span_seconds,
            "block_retry_reasons": dict(reason_counts.most_common()),
            "task_event_counts": dict(event_kind_counts),
            "operational_event_counts": operational_event_interest,
        },
        "correlation": {
            "runs_total": run_count,
            "runs_with_worker_session_id": runs_with_sid,
            "runs_correlated": runs_correlated,
            "coverage_pct": coverage_pct,
            "unique_session_ids": len(session_ids),
            "resolved_unique_sessions": len(resolved_unique_sessions),
            "missing_session_ids": sorted(missing_session_ids),
            "ambiguous_session_ids": sorted(ambiguous_session_ids),
            "runs_without_worker_session_id": run_count - runs_with_sid,
        },
        "totals": aggregate,
        "retry_resumption_overhead": {
            "definition": "Unique correlated sessions attached to every task run after that task's first run.",
            "session_count": len(retry_session_ids),
            "metrics": retry_metrics,
        },
        "gut": gut,
        "breakdown": {
            "sessions_by_profile": dict(sorted(profiles.items())),
            "sessions_by_model": dict(sorted(models.items())),
            "sessions_by_billing_provider": dict(sorted(providers.items())),
        },
        "rankings": {
            "top_token_consumers": top_token_consumers,
            "top_repeated_operations": gut["top_repeated_operations"],
            "top_resumed_cards": top_resumed_cards,
        },
        "usd": {
            "policy": "best_effort_no_pricing_guess",
            "known_cost_usd": aggregate["known_cost_usd"],
            "known_cost_sessions": aggregate["known_cost_sessions"],
            "unknown_cost_sessions": aggregate["unknown_cost_sessions"],
            "coverage_pct": usd_coverage_pct,
        },
        "notes": [
            "Token counters come from Hermes sessions and are not re-tokenized or estimated.",
            "observed_token_volume = input + output + cache_read + cache_write + reasoning; components remain separately authoritative.",
            "Worker runtime is summed from task_runs.started_at/ended_at; session ended_at is not used for duration.",
            "Resumption count is run-level: every task run after that task's first run. Its kind is classified from the previous run outcome.",
            "Block/retry reason labels come from structured run metadata/error or explicit summary prefixes; missing reasons are not invented.",
            "GUT operations come from task-run structured test metadata plus gwrm-gut-runner audit/cache; missing counts/durations remain null.",
            "GWRM observed_wall_ms is audit-event wall time; duration_ms is only used when structured task/cache evidence provides it.",
            "GWRM terminal_cache_hit/reused_existing_operation/wait_existing are counted as reuse signals, not new test operations.",
            "USD is reported only when Hermes marks actual/estimated session cost as available; missing pricing remains unknown.",
            "Runs lacking metadata.worker_session_id remain explicitly unavailable; no temporal/profile heuristic is used.",
            "Aggregate token totals deduplicate repeated worker_session_id values to avoid double counting.",
        ],
        "cards": card_rows,
        "runs": run_rows,
    }
    return report



def _persist_report(report: dict[str, Any], report_dir: Path) -> tuple[str, str]:
    report_dir.mkdir(parents=True, exist_ok=True)
    task_id = str(report["scope"]["root_task_id"])
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime(report["generated_at_epoch"]))
    path = report_dir / f"{task_id}_{stamp}.json"
    latest = report_dir / f"{task_id}_latest.json"
    payload = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path.write_text(payload, encoding="utf-8")
    latest.write_text(payload, encoding="utf-8")
    return str(path), str(latest)


def _summary_result(report: dict[str, Any], report_path: Optional[str], detail_level: str) -> dict[str, Any]:
    result = {
        "task_id": report["scope"]["root_task_id"],
        "phase_id": report["scope"].get("phase_id"),
        "task_count": report["scope"]["task_count"],
        "run_count": report["operations"]["run_count"],
        "extra_attempts": report["operations"]["extra_attempts"],
        "worker_runtime_seconds": report["operations"]["worker_runtime_seconds"],
        "correlation_coverage_pct": report["correlation"]["coverage_pct"],
        "tokens": {
            k: report["totals"][k]
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "reasoning_tokens",
                "observed_token_volume",
            )
        },
        "api_call_count": report["totals"]["api_call_count"],
        "tool_call_count": report["totals"]["tool_call_count"],
        "known_cost_usd": report["usd"]["known_cost_usd"],
        "usd_coverage_pct": report["usd"]["coverage_pct"],
        "retry_resumption_overhead_token_volume": report["retry_resumption_overhead"]["metrics"]["observed_token_volume"],
        "resumption_count": report["operations"]["resumption_count"],
        "gut": {
            "operation_count": report["gut"]["operation_count"],
            "total_tests": report["gut"]["total_tests"],
            "total_asserts": report["gut"]["total_asserts"],
            "total_known_duration_ms": report["gut"]["total_known_duration_ms"],
            "reuse": report["gut"]["reuse"],
        },
        "top_token_consumers": report["rankings"]["top_token_consumers"],
        "top_repeated_operations": report["rankings"]["top_repeated_operations"],
        "sessions_by_profile": report["breakdown"]["sessions_by_profile"],
        "sessions_by_model": report["breakdown"]["sessions_by_model"],
        "report_path": report_path,
    }
    if detail_level == "cards":
        result["cards"] = report["cards"]
    return result


def _handler(args: dict[str, Any]) -> str:
    cfg = _cfg()
    task_id = str(args.get("task_id") or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not task_id:
        return _err("task_id is required when HERMES_KANBAN_TASK is unavailable")
    include_desc = bool(args.get("include_descendants", True))
    include_legacy = bool(args.get("include_legacy_body_fallback", True))
    detail_level = str(args.get("detail_level") or "summary")
    persist = bool(args.get("persist_report", True))
    report_dir = Path(str(cfg["report_dir"]))
    try:
        from hermes_cli.kanban_db_connect import connect_closing

        with connect_closing() as conn:
            report = _build_report(
                conn,
                root_task_id=task_id,
                profiles_root=Path(str(cfg["profiles_root"])),
                include_descendants=include_desc,
                include_legacy_body_fallback=include_legacy,
                max_cards=int(cfg["max_cards"]),
                max_sessions=int(cfg["max_sessions"]),
                top_n=int(cfg["top_n"]),
                gwrm_events_path=Path(str(cfg["gwrm_events_path"])),
                gwrm_cache_path=Path(str(cfg["gwrm_cache_path"])),
            )
        report_path = None
        latest_path = None
        if persist:
            report_path, latest_path = _persist_report(report, report_dir)
        _audit(
            {
                "event": "report_generated",
                "task_id": task_id,
                "task_count": report["scope"]["task_count"],
                "run_count": report["operations"]["run_count"],
                "coverage_pct": report["correlation"]["coverage_pct"],
                "report_path": report_path,
            },
            report_dir,
        )
        return _ok(**_summary_result(report, report_path, detail_level), latest_path=latest_path)
    except Exception as e:
        _audit({"event": "report_failed", "task_id": task_id, "error": str(e)[:1200]}, report_dir)
        return _err(str(e), task_id=task_id)


def _is_root_like(conn: sqlite3.Connection, task_id: str) -> bool:
    """Return True for a phase/root-like orchestrator card without semantic inference."""
    if _table_exists(conn, "task_links"):
        row = conn.execute("SELECT 1 FROM task_links WHERE parent_id=? LIMIT 1", (task_id,)).fetchone()
        if row:
            return True
    hint = _root_phase_hint(conn, task_id)
    return bool(hint.get("checkpoint_version") is not None or hint.get("phase_id"))


def _on_session_end(
    session_id: str = "",
    task_id: str = "",
    completed: bool = True,
    failed: bool = False,
    interrupted: bool = False,
    **_: Any,
) -> None:
    """Persist a final root roll-up after Hermes has persisted the closing turn.

    Hermes invokes on_session_end after ``agent._persist_session``. The callback is
    observer-only: it reads Kanban/session/GWRM telemetry and writes only to the
    plugin report directory. It never mutates Kanban or a project repository.
    """
    if not _available():
        return
    cfg = _cfg()
    if not bool(cfg.get("auto_report_on_session_end", True)):
        return
    resolved_task_id = str(task_id or os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    if not resolved_task_id:
        return
    report_dir = Path(str(cfg["report_dir"]))
    try:
        from hermes_cli.kanban_db_connect import connect_closing

        with connect_closing() as conn:
            tasks = _load_tasks(conn)
            task = tasks.get(resolved_task_id)
            if task is None:
                return
            # Only the terminal root/phase turn should emit an automatic post-phase report.
            if str(task.get("status") or "").lower() != "done":
                return
            if not _is_root_like(conn, resolved_task_id):
                return
            report = _build_report(
                conn,
                root_task_id=resolved_task_id,
                profiles_root=Path(str(cfg["profiles_root"])),
                include_descendants=True,
                include_legacy_body_fallback=True,
                max_cards=int(cfg["max_cards"]),
                max_sessions=int(cfg["max_sessions"]),
                top_n=int(cfg["top_n"]),
                gwrm_events_path=Path(str(cfg["gwrm_events_path"])),
                gwrm_cache_path=Path(str(cfg["gwrm_cache_path"])),
            )
        report["generation_trigger"] = {
            "type": "on_session_end",
            "session_id": str(session_id or "") or None,
            "completed": bool(completed),
            "failed": bool(failed),
            "interrupted": bool(interrupted),
        }
        report_path, latest_path = _persist_report(report, report_dir)
        _audit(
            {
                "event": "auto_post_phase_report_generated",
                "task_id": resolved_task_id,
                "session_id": str(session_id or "") or None,
                "finality": report.get("finality", {}).get("status"),
                "task_count": report["scope"]["task_count"],
                "run_count": report["operations"]["run_count"],
                "report_path": report_path,
                "latest_path": latest_path,
            },
            report_dir,
        )
    except Exception as exc:
        _audit(
            {
                "event": "auto_post_phase_report_failed",
                "task_id": resolved_task_id,
                "session_id": str(session_id or "") or None,
                "error": f"{type(exc).__name__}: {exc}"[:1200],
            },
            report_dir,
        )


def register(ctx: Any) -> None:
    ctx.register_hook("on_session_end", _on_session_end)
    ctx.register_tool(
        name="operational_cost_report",
        toolset=TOOLSET,
        schema=REPORT_SCHEMA,
        handler=_handler,
        check_fn=_available,
        emoji="📊",
    )


def _main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Generate Hermes operational cost telemetry without an LLM call.")
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--no-descendants", action="store_true")
    ap.add_argument("--no-legacy-body-fallback", action="store_true")
    ap.add_argument("--no-persist", action="store_true")
    ap.add_argument("--detail", choices=("summary", "cards"), default="summary")
    ns = ap.parse_args(argv)
    cfg = _cfg()
    from hermes_cli.kanban_db_connect import connect_closing

    with connect_closing() as conn:
        report = _build_report(
            conn,
            root_task_id=ns.task_id,
            profiles_root=Path(str(cfg["profiles_root"])),
            include_descendants=not ns.no_descendants,
            include_legacy_body_fallback=not ns.no_legacy_body_fallback,
            max_cards=int(cfg["max_cards"]),
            max_sessions=int(cfg["max_sessions"]),
            top_n=int(cfg["top_n"]),
            gwrm_events_path=Path(str(cfg["gwrm_events_path"])),
            gwrm_cache_path=Path(str(cfg["gwrm_cache_path"])),
        )
    path = None
    if not ns.no_persist:
        path, _ = _persist_report(report, Path(str(cfg["report_dir"])))
    print(json.dumps(_summary_result(report, path, ns.detail), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
