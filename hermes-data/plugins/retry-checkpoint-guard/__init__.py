from __future__ import annotations

import hashlib
import json
import os
import logging
import subprocess
import threading
import time
from pathlib import Path
from typing import Any


LOG = logging.getLogger(
    "hermes.plugins.retry_checkpoint_guard"
)

_ALLOWED_PROFILES = {
    "implementation-worker",
    "implementation-architect",
}

_PENDING: dict[str, dict[str, Any]] = {}
_PENDING_LOCK = threading.Lock()

_ORIGINAL_GUARD = None


def _audit(payload: dict[str, Any]) -> None:
    try:
        path = Path(
            "/opt/data/logs/"
            "retry-checkpoint-guard/events.jsonl"
        )

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        record = {
            "ts": int(time.time()),
            **payload,
        }

        with path.open(
            "a",
            encoding="utf-8",
        ) as stream:
            stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except Exception:
        pass


def _git_status(workspace: Path) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(workspace),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except Exception as exc:
        return {
            "ok": False,
            "error": (
                f"{type(exc).__name__}: {exc}"
            ),
            "entries": [],
        }

    if proc.returncode != 0:
        return {
            "ok": False,
            "error": (
                proc.stderr
                or proc.stdout
                or f"git rc={proc.returncode}"
            )[-1000:],
            "entries": [],
        }

    entries = [
        line
        for line in proc.stdout.splitlines()
        if line.strip()
    ]
    status_text = proc.stdout or ""
    head_proc = subprocess.run(
        ["git", "-C", str(workspace), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
        check=False,
    )
    head = head_proc.stdout.strip() if head_proc.returncode == 0 else None

    return {
        "ok": True,
        "entries": entries,
        "head": head,
        "status_sha256": hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
    }


# HERMES_OPERATIONAL_HARDENING_2026_09_03: allow exactly one governance-authorized dirty retry fingerprint.
def _governor_state_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home()).resolve(strict=False)
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", "/opt/data")).resolve(strict=False)
    if home.name == "execution-governor":
        governor = home
    elif home.parent.name == "profiles":
        governor = home.parent / "execution-governor"
    else:
        governor = home / "profiles" / "execution-governor"
    return governor / "governance" / "state.json"


def _authorized_recovery(task_id: str, workspace: Path, status: dict[str, Any]) -> dict[str, Any] | None:
    try:
        state = json.loads(_governor_state_path().read_text(encoding="utf-8"))
        task = (state.get("tasks") or {}).get(task_id) or {}
        auth = task.get("authorized_recovery_checkpoint")
        if not isinstance(auth, dict) or auth.get("state") != "AUTHORIZED":
            return None
        if int(auth.get("expires_at") or 0) < int(time.time()):
            return None
        if str(Path(str(auth.get("workspace") or "")).resolve(strict=False)) != str(workspace.resolve(strict=False)):
            return None
        if str(auth.get("head") or "") != str(status.get("head") or ""):
            return None
        if str(auth.get("status_sha256") or "") != str(status.get("status_sha256") or ""):
            return None
        return auth
    except Exception:
        return None


def _inspect_retry_checkpoint(
    conn,
    task_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        SELECT id,
               status,
               assignee,
               workspace_kind,
               workspace_path
          FROM tasks
         WHERE id = ?
        """,
        (task_id,),
    ).fetchone()

    if row is None:
        return None

    if str(row["status"] or "") != "ready":
        return None

    assignee = str(
        row["assignee"] or ""
    )

    if assignee not in _ALLOWED_PROFILES:
        return None

    if str(
        row["workspace_kind"] or ""
    ) != "worktree":
        return None

    run_count = int(
        conn.execute(
            """
            SELECT COUNT(*)
              FROM task_runs
             WHERE task_id = ?
            """,
            (task_id,),
        ).fetchone()[0]
    )

    # First execution is not a retry.
    if run_count < 1:
        return None

    raw_workspace = str(
        row["workspace_path"] or ""
    ).strip()

    if not raw_workspace:
        return None

    workspace = Path(
        raw_workspace
    ).resolve(strict=False)

    # Workspace resolution/provisioning failures belong
    # to the existing Hermes/Guardian paths.
    if not workspace.is_dir():
        return None

    status = _git_status(workspace)

    # Fail closed if a retry worktree cannot even be
    # inspected reliably.
    if not status["ok"]:
        return {
            "task_id": task_id,
            "workspace": str(workspace),
            "run_count": run_count,
            "guard_reason":
                "retry_checkpoint_probe_failed",
            "probe_error":
                status.get("error"),
            "entries": [],
        }

    entries = status["entries"]

    if not entries:
        return None

    authorization = _authorized_recovery(task_id, workspace, status)
    if authorization is not None:
        return {
            "task_id": task_id,
            "workspace": str(workspace),
            "run_count": run_count,
            "guard_reason": "authorized_retry_checkpoint",
            "authorized_recovery": True,
            "resume_epoch": authorization.get("resume_epoch"),
            "entries": entries,
        }

    return {
        "task_id": task_id,
        "workspace": str(workspace),
        "run_count": run_count,
        "guard_reason":
            "dirty_retry_checkpoint",
        "entries": entries,
    }


def _remember(state: dict[str, Any]) -> None:
    task_id = state["task_id"]

    with _PENDING_LOCK:
        _PENDING[task_id] = state


def _guarded_respawn(
    conn,
    task_id: str,
    *args,
    **kwargs,
):
    # Preserve every native Hermes guard first.
    native_reason = _ORIGINAL_GUARD(
        conn,
        task_id,
        *args,
        **kwargs,
    )

    if native_reason is not None:
        return native_reason

    lane = kwargs.get(
        "lane",
        "ready",
    )

    # Review dispatch has different semantics.
    if lane != "ready":
        return None

    state = _inspect_retry_checkpoint(
        conn,
        task_id,
    )

    if state is None:
        return None

    if state.get("authorized_recovery"):
        _audit({
            "event": "authorized_retry_checkpoint_allowed",
            **state,
            "entries": state.get("entries", [])[:50],
        })
        return None

    _remember(state)

    _audit({
        "event":
            "pre_spawn_retry_guard",
        **state,
        "entries":
            state.get("entries", [])[:50],
    })

    # Returning any reason prevents claim/spawn
    # during THIS dispatcher tick.
    return state["guard_reason"]


_guarded_respawn._retry_checkpoint_guard = True


def _build_reason(
    state: dict[str, Any],
) -> str:
    if (
        state["guard_reason"]
        == "retry_checkpoint_probe_failed"
    ):
        detail = (
            state.get("probe_error")
            or "unknown Git status error"
        )

        return (
            "RETRY_CHECKPOINT_GUARD: "
            "automatic redispatch refused because "
            "the existing retry worktree could not "
            f"be inspected safely. {detail[:700]}"
        )

    return (
        "RETRY_CHECKPOINT_GUARD: "
        "automatic redispatch refused because this "
        "task already has a prior run and its "
        "worktree contains uncommitted/untracked "
        "state. Preserve the checkpoint. "
        "The orchestrator must inspect and route "
        "recovery/replacement before another worker "
        "is dispatched. Do not reset, clean, delete "
        "or reprovision the worktree automatically."
    )


def _build_comment(
    state: dict[str, Any],
) -> str:
    entries = state.get(
        "entries",
        [],
    )

    sample = "\n".join(
        entries[:30]
    )

    if len(entries) > 30:
        sample += (
            f"\n... {len(entries) - 30} "
            "additional entries omitted"
        )

    return (
        "WORKTREE_RETRY_CHECKPOINT_GUARD\n"
        "automatic_redispatch: refused\n"
        f"reason: {state['guard_reason']}\n"
        f"workspace: {state['workspace']}\n"
        f"prior_run_count: {state['run_count']}\n"
        f"dirty_entries: {len(entries)}\n"
        "required_action: orchestrator must inspect "
        "the preserved checkpoint and route a safe "
        "recovery/replacement before unblocking.\n"
        "do_not: reset/clean/delete/reprovision "
        "automatically.\n"
        + (
            "git_status_sample:\n"
            + sample
            + "\n"
            if sample
            else ""
        )
    )


def _flush_pending(
    board=None,
    dry_run=False,
    **kwargs,
):
    del kwargs

    if dry_run:
        return None

    with _PENDING_LOCK:
        pending_ids = list(
            _PENDING.keys()
        )

    if not pending_ids:
        return True

    from hermes_cli import (
        kanban_db as kb,
    )

    handled = []

    with kb.connect_closing(
        board=board,
    ) as conn:
        for task_id in pending_ids:
            # Re-evaluate after the dispatcher lock has
            # been released. State may have changed.
            fresh = _inspect_retry_checkpoint(
                conn,
                task_id,
            )

            if fresh is None:
                handled.append(task_id)
                continue

            if fresh.get("authorized_recovery"):
                _audit({
                    "event": "authorized_retry_checkpoint_allowed",
                    **fresh,
                    "entries": fresh.get("entries", [])[:50],
                })
                handled.append(task_id)
                continue

            reason = _build_reason(
                fresh
            )

            blocked = kb.block_task(
                conn,
                task_id,
                reason=reason,
                kind="needs_input",
            )

            if not blocked:
                # Do not discard the pending guard.
                # Next tick still refuses the spawn.
                continue

            try:
                kb.add_comment(
                    conn,
                    task_id,
                    author=
                        "retry-checkpoint-guard",
                    body=_build_comment(fresh),
                )
            except Exception as exc:
                _audit({
                    "event":
                        "comment_failed",
                    "task_id": task_id,
                    "error":
                        f"{type(exc).__name__}: {exc}",
                })

            _audit({
                "event":
                    "retry_checkpoint_blocked",
                **fresh,
                "entries":
                    fresh.get("entries", [])[:50],
            })

            handled.append(task_id)

    if handled:
        with _PENDING_LOCK:
            for task_id in handled:
                _PENDING.pop(
                    task_id,
                    None,
                )

    return True


def register(ctx):
    global _ORIGINAL_GUARD

    from hermes_cli import (
        kanban_db as kb,
    )

    # HERMES_V021_DISPATCH_GUARD_2026_09_07
    import importlib
    kbd = importlib.import_module("hermes_cli.kanban_db_dispatch")
    current = kbd.check_respawn_guard

    if getattr(
        current,
        "_retry_checkpoint_guard",
        False,
    ):
        _ORIGINAL_GUARD = getattr(
            current,
            "_retry_checkpoint_original",
            None,
        ) or _ORIGINAL_GUARD

    else:
        _ORIGINAL_GUARD = current

        _guarded_respawn._retry_checkpoint_original = (
            current
        )

        kbd.check_respawn_guard = (
            _guarded_respawn
        )
        kb.check_respawn_guard = (
            _guarded_respawn
        )

        _audit({
            "event":
                "respawn_guard_patched",
        })

    ctx.register_hook(
        "on_kanban_dispatch_tick",
        _flush_pending,
    )
