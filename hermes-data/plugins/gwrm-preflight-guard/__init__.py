"""TODO10: GWRM preflight before an LLM worker/session exists.

This plugin does not modify Hermes core.  It wraps the current
``kanban_db_dispatch._dispatch_lane_task`` function and performs one narrow
pre-dispatch gate:

* Cards with explicit ``gwrm_required: true`` must observe a healthy GWRM
  Control API before the wrapped dispatcher lane is allowed to proceed.
* When GWRM is unavailable, the wrapper returns before delegating to Hermes.
  Therefore Hermes has not yet claimed the card, allocated a run, spawned a
  worker, or created an LLM session through that lane.
* Cards with ``gwrm_required: false`` are allowed to proceed normally.
* Implementation cards assigned to ``implementation-worker`` or
  ``implementation-architect`` without an explicit marker are deferred before
  dispatcher delegation.  This makes the pre-LLM contract fail-closed.

The guard is deliberately conservative: it does not provision worktrees,
activate runtimes, mutate Git, change card status, or write Kanban comments.
A future dispatcher tick retries naturally after the external service recovers.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_PLUGIN = "gwrm-preflight-guard"
_ORIGINAL_DISPATCH_LANE = None
_LAST_AUDIT: dict[tuple[str, str], float] = {}

_TRUE = {"true", "yes", "1", "on"}
_FALSE = {"false", "no", "0", "off"}
_IMPLEMENTATION_ASSIGNEES = {
    "implementation-worker",
    "implementation-architect",
}


def _config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = cfg.get("gwrm_preflight_guard") or {}
        return {
            "enabled": bool(section.get("enabled", True)),
            "health_url": str(
                section.get(
                    "health_url",
                    "http://host.docker.internal:8130/health",
                )
            ),
            "timeout_seconds": float(section.get("timeout_seconds", 2.0)),
            "audit_interval_seconds": int(
                section.get("audit_interval_seconds", 60)
            ),
        }
    except Exception:
        # Fail closed for GWRM-required cards and implementation cards missing an explicit requirement marker.
        return {
            "enabled": True,
            "health_url": "http://host.docker.internal:8130/health",
            "timeout_seconds": 2.0,
            "audit_interval_seconds": 60,
        }


def _audit(task_id: str, event: str, **extra: Any) -> None:
    cfg = _config()
    interval = max(1, int(cfg["audit_interval_seconds"]))
    now = time.time()
    key = (str(task_id), str(event))
    if now - _LAST_AUDIT.get(key, 0.0) < interval:
        return
    _LAST_AUDIT[key] = now

    try:
        path = Path("/opt/data/logs/gwrm-preflight-guard/events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "ts": int(now),
            "plugin": _PLUGIN,
            "task_id": str(task_id),
            "event": str(event),
            **extra,
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except Exception:
        pass


def _row_get(row: Any, name: str, default: Any = None) -> Any:
    try:
        keys = row.keys()
        if name in keys:
            return row[name]
    except Exception:
        pass
    try:
        return getattr(row, name)
    except Exception:
        return default


def _task_body(conn, row: Any) -> str:
    body = _row_get(row, "body")
    if body is not None:
        return str(body)

    task_id = _row_get(row, "id")
    if not task_id:
        return ""

    try:
        cols = {
            str(r[1])
            for r in conn.execute("PRAGMA table_info(tasks)").fetchall()
        }
        if "body" not in cols:
            return ""
        found = conn.execute(
            "SELECT body FROM tasks WHERE id = ?",
            (str(task_id),),
        ).fetchone()
        if found is None:
            return ""
        try:
            return str(found["body"] or "")
        except Exception:
            return str(found[0] or "")
    except Exception:
        return ""


def _explicit_bool_marker(text: str, key: str) -> bool | None:
    pattern = re.compile(
        rf"(?mi)^\s*{re.escape(key)}\s*:\s*"
        r"(true|false|yes|no|1|0|on|off)\s*$"
    )
    found = list(pattern.finditer(text or ""))
    if not found:
        return None
    raw = found[-1].group(1).strip().lower()
    if raw in _TRUE:
        return True
    if raw in _FALSE:
        return False
    return None


def _gwrm_requirement(conn, row: Any) -> bool | None:
    return _explicit_bool_marker(
        _task_body(conn, row),
        "gwrm_required",
    )


def _check_gwrm_health() -> tuple[bool, str]:
    cfg = _config()
    if not cfg["enabled"]:
        return True, "guard_disabled"

    request = urllib.request.Request(
        cfg["health_url"],
        method="GET",
        headers={"Accept": "application/json,text/plain,*/*"},
    )
    try:
        with urllib.request.urlopen(
            request,
            timeout=max(0.2, float(cfg["timeout_seconds"])),
        ) as response:
            status = int(getattr(response, "status", 200) or 200)
            # Consume only a bounded prefix.  HTTP success is the TODO10
            # liveness contract; deeper half-alive detection is separate work.
            response.read(4096)
            if 200 <= status < 300:
                return True, f"http_{status}"
            return False, f"http_{status}"
    except urllib.error.HTTPError as exc:
        return False, f"http_{int(exc.code)}"
    except Exception as exc:
        return False, f"{type(exc).__name__}:{str(exc)[:160]}"


def _record_guard_result(result: Any, task_id: str, reason: str) -> None:
    # Best-effort diagnostics only; never require a specific Hermes result
    # implementation to preserve compatibility across minor versions.
    for attr in ("preflight_guarded", "respawn_guarded"):
        try:
            bucket = getattr(result, attr)
            bucket.append((task_id, reason))
            return
        except Exception:
            continue


def _guarded_dispatch_lane(conn, row, assignee, result, *args, **kwargs):
    task_id = str(_row_get(row, "id", "") or "")
    assignee_name = str(assignee or "")
    cfg = _config()

    if cfg["enabled"]:
        requirement = _gwrm_requirement(conn, row)

        if (
            assignee_name in _IMPLEMENTATION_ASSIGNEES
            and requirement is None
        ):
            # Fail closed before Hermes can claim/spawn.  The orchestrator must
            # explicitly classify implementation work as GWRM-required or not.
            _record_guard_result(
                result,
                task_id,
                "gwrm_requirement_missing",
            )
            _audit(
                task_id,
                "pre_llm_dispatch_deferred",
                assignee=assignee_name,
                reason="gwrm_requirement_missing",
            )
            return False

        if requirement is True:
            healthy, detail = _check_gwrm_health()
            if not healthy:
                # IMPORTANT: do not block/requeue/comment/claim. Returning
                # before the wrapped dispatcher preserves the pre-LLM invariant.
                _record_guard_result(
                    result,
                    task_id,
                    "gwrm_preflight_unavailable",
                )
                _audit(
                    task_id,
                    "pre_llm_dispatch_deferred",
                    assignee=assignee_name,
                    reason="gwrm_unavailable",
                    health=detail,
                )
                return False

            _audit(
                task_id,
                "pre_llm_dispatch_allowed",
                assignee=assignee_name,
                health=detail,
            )

    return _ORIGINAL_DISPATCH_LANE(
        conn,
        row,
        assignee,
        result,
        *args,
        **kwargs,
    )


_guarded_dispatch_lane._todo10_pre_llm_guard = True
_guarded_dispatch_lane._hermes_dispatch_guard = "gwrm-preflight-guard"


_DISPATCH_ORIGINAL_ATTRS = (
    "_todo10_pre_llm_guard_original",
    "_kanban_mode_guard_original",
)


def _next_dispatch_wrapper(candidate):
    for attr in _DISPATCH_ORIGINAL_ATTRS:
        original = getattr(candidate, attr, None)
        if callable(original) and original is not candidate:
            return original
    return None


def _find_dispatch_wrapper(candidate, marker_attr: str):
    current = candidate
    seen: set[int] = set()

    while callable(current):
        ident = id(current)
        if ident in seen:
            raise RuntimeError("cyclic dispatcher wrapper chain")
        seen.add(ident)

        if getattr(current, marker_attr, False):
            return current

        current = _next_dispatch_wrapper(current)

    return None


def _ensure_patch() -> None:
    global _ORIGINAL_DISPATCH_LANE

    from hermes_cli import kanban_db_dispatch as dispatch

    current = dispatch._dispatch_lane_task
    existing = _find_dispatch_wrapper(
        current,
        "_todo10_pre_llm_guard",
    )

    if existing is not None:
        original = getattr(
            existing,
            "_todo10_pre_llm_guard_original",
            None,
        )
        if not callable(original) or original is existing:
            raise RuntimeError(
                "invalid TODO10 dispatcher wrapper original"
            )
        # Needed when the module is re-executed/reloaded while an older
        # wrapper function is still installed in the shared dispatcher chain.
        _ORIGINAL_DISPATCH_LANE = original
        return

    _ORIGINAL_DISPATCH_LANE = current
    _guarded_dispatch_lane._todo10_pre_llm_guard_original = current
    dispatch._dispatch_lane_task = _guarded_dispatch_lane


def _health_hook(**kwargs):
    # Do not mutate a shared monkey-patch chain on every dispatcher tick.
    # register() installs/reconciles the wrapper once. Repeated per-tick
    # rewrapping can form a cycle with kanban-mode-guard.
    return True


def register(ctx) -> None:
    _ensure_patch()
    ctx.register_hook("on_kanban_dispatch_tick", _health_hook)
