"""Environment-wide Kanban goal-mode prohibition.

No Hermes core files are modified.  The plugin enforces the policy at three
layers:

1. Persistent SQLite triggers reject creation or conversion to goal_mode=1.
   This protects CLI/dashboard writers even when that process does not load
   Python plugins.
2. kanban_db.create_task is wrapped in plugin-loaded processes for an early,
   explicit error.
3. The dispatcher lane is guarded before claim/spawn so any legacy/external
   goal-mode row is blocked instead of executed.

Existing historical goal-mode rows are not rewritten.  The UPDATE trigger only
fires when the goal_mode column itself is changed, so old completed history
remains readable/manageable.
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

_PLUGIN = "kanban-mode-guard"
_TRIGGER_INSERT = "kanban_mode_guard_forbid_goal_insert"
_TRIGGER_UPDATE = "kanban_mode_guard_forbid_goal_update"
_ORIGINAL_CREATE = None
_ORIGINAL_DISPATCH_LANE = None


def _audit(payload: dict[str, Any]) -> None:
    try:
        path = Path("/opt/data/logs/kanban-mode-guard/events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), **payload},
                               ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _enabled() -> bool:
    try:
        from hermes_cli.config import load_config
        cfg = load_config() or {}
        section = cfg.get("kanban_mode_guard") or {}
        return bool(section.get("forbid_goal_mode", True))
    except Exception:
        # Fail closed: if the plugin is explicitly enabled but config cannot be
        # read, preserve the safety policy.
        return True


def _db_path(board: str | None = None) -> Path:
    from hermes_cli import kanban_db as kb
    return Path(kb.kanban_db_path(board=board)).resolve(strict=False)


def _known_db_paths(board: str | None = None) -> list[Path]:
    paths: set[Path] = set()
    try:
        paths.add(_db_path(board))
    except Exception:
        pass

    # Current deployment keeps the canonical board under /opt/data/kanban.db.
    # Also cover multi-board layouts if present.
    for pattern in (
        "/opt/data/kanban.db",
        "/opt/data/kanban/boards/*/kanban.db",
        "/opt/data/kanban/boards/*/*.db",
    ):
        for raw in Path("/").glob(pattern.lstrip("/")):
            if raw.is_file():
                paths.add(raw.resolve(strict=False))
    return sorted(paths)


def _install_triggers(path: Path) -> None:
    if not path.exists():
        return
    conn = sqlite3.connect(str(path), timeout=10)
    try:
        conn.execute("PRAGMA busy_timeout=10000")
        if _enabled():
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {_TRIGGER_INSERT}
                BEFORE INSERT ON tasks
                WHEN COALESCE(NEW.goal_mode, 0) <> 0
                BEGIN
                  SELECT RAISE(ABORT,
                    'KANBAN_MODE_GUARD: goal_mode is forbidden by environment policy');
                END
                """
            )
            conn.execute(
                f"""
                CREATE TRIGGER IF NOT EXISTS {_TRIGGER_UPDATE}
                BEFORE UPDATE OF goal_mode ON tasks
                WHEN COALESCE(NEW.goal_mode, 0) <> 0
                BEGIN
                  SELECT RAISE(ABORT,
                    'KANBAN_MODE_GUARD: goal_mode is forbidden by environment policy');
                END
                """
            )
        else:
            conn.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_INSERT}")
            conn.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER_UPDATE}")
        conn.commit()
    finally:
        conn.close()


def _ensure_db_guards(board: str | None = None) -> None:
    for path in _known_db_paths(board):
        try:
            _install_triggers(path)
        except Exception as exc:
            _audit({
                "event": "trigger_install_failed",
                "db": str(path),
                "error": f"{type(exc).__name__}: {exc}"[:1000],
            })
            raise


def _guarded_create_task(conn, *args, **kwargs):
    if _enabled() and bool(kwargs.get("goal_mode", False)):
        _audit({
            "event": "goal_mode_create_rejected",
            "assignee": kwargs.get("assignee"),
            "title": str(kwargs.get("title") or "")[:200],
        })
        raise ValueError(
            "KANBAN_MODE_GUARD: goal_mode=true is forbidden by environment policy"
        )
    return _ORIGINAL_CREATE(conn, *args, **kwargs)


_guarded_create_task._kanban_mode_guard = True


def _guarded_dispatch_lane(conn, row, assignee, result, *args, **kwargs):
    task_id = row["id"]
    if _enabled():
        dbrow = conn.execute(
            "SELECT goal_mode, status FROM tasks WHERE id = ?",
            (task_id,),
        ).fetchone()
        if dbrow is not None and bool(dbrow["goal_mode"]):
            dry_run = bool(kwargs.get("dry_run", False))
            if not dry_run:
                from hermes_cli import kanban_db as kb
                reason = (
                    "KANBAN_MODE_GUARD: goal_mode=true is forbidden. "
                    "The card was blocked before claim/spawn and must be "
                    "recreated or administratively corrected with goal_mode=false."
                )
                try:
                    kb.block_task(conn, task_id, reason=reason, kind="needs_input")
                except Exception:
                    _audit({
                        "event": "legacy_goal_block_failed",
                        "task_id": task_id,
                    })
                    raise
                try:
                    kb.add_comment(
                        conn,
                        task_id,
                        author=_PLUGIN,
                        body=(
                            "KANBAN_MODE_GUARD_BLOCKED\n"
                            "reason: goal_mode_forbidden\n"
                            "required_action: recreate_or_correct_with_goal_mode_false\n"
                        ),
                    )
                except Exception:
                    pass
            try:
                result.respawn_guarded.append((task_id, "goal_mode_forbidden"))
            except Exception:
                pass
            _audit({
                "event": "goal_mode_dispatch_blocked",
                "task_id": task_id,
                "assignee": assignee,
                "dry_run": dry_run,
            })
            return False

    return _ORIGINAL_DISPATCH_LANE(
        conn, row, assignee, result, *args, **kwargs
    )


_guarded_dispatch_lane._kanban_mode_guard = True
_guarded_dispatch_lane._hermes_dispatch_guard = "kanban-mode-guard"


_DISPATCH_ORIGINAL_ATTRS = (
    "_kanban_mode_guard_original",
    "_todo10_pre_llm_guard_original",
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


def _ensure_patches() -> None:
    global _ORIGINAL_CREATE, _ORIGINAL_DISPATCH_LANE

    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as dispatch

    current_create = kb.create_task
    if not getattr(current_create, "_kanban_mode_guard", False):
        _ORIGINAL_CREATE = current_create
        _guarded_create_task._kanban_mode_guard_original = current_create
        kb.create_task = _guarded_create_task
    else:
        original_create = getattr(
            current_create,
            "_kanban_mode_guard_original",
            None,
        )
        if not callable(original_create) or original_create is current_create:
            raise RuntimeError(
                "invalid kanban-mode create_task wrapper original"
            )
        _ORIGINAL_CREATE = original_create

    current_lane = dispatch._dispatch_lane_task
    existing = _find_dispatch_wrapper(
        current_lane,
        "_kanban_mode_guard",
    )

    if existing is not None:
        original_lane = getattr(
            existing,
            "_kanban_mode_guard_original",
            None,
        )
        if not callable(original_lane) or original_lane is existing:
            raise RuntimeError(
                "invalid kanban-mode dispatcher wrapper original"
            )
        # Needed when plugin discovery/reload re-executes this module while an
        # older wrapper remains inside another guard's shared wrapper chain.
        _ORIGINAL_DISPATCH_LANE = original_lane
        return

    _ORIGINAL_DISPATCH_LANE = current_lane
    _guarded_dispatch_lane._kanban_mode_guard_original = current_lane
    dispatch._dispatch_lane_task = _guarded_dispatch_lane


def _health_hook(**kwargs):
    # DB triggers are persistent state and may be reconciled every tick.
    # Python monkey-patches are intentionally NOT rebuilt here: doing so while
    # gwrm-preflight-guard is also installed can create a cyclic wrapper chain.
    _ensure_db_guards(kwargs.get("board"))
    return True


def register(ctx) -> None:
    _ensure_patches()
    _ensure_db_guards(None)
    ctx.register_hook("on_kanban_dispatch_tick", _health_hook)
