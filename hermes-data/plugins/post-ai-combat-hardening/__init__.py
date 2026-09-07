"""Post AI-COMBAT operational hardening overlay.

HERMES_POST_AI_COMBAT_HARDENING_2026_09_07

Narrow responsibilities:
1) remove only proven transient untracked Godot .gd.uid artifacts before
   retry fingerprinting / Worktree Guardian verify;
2) prevent a worker/architect with explicit ``gwrm_required: true`` from
   spawning while the GWRM control plane is unavailable.

No Kanban DB mutation is performed by this plugin. It composes with the native
respawn guard chain and leaves the card READY so a later dispatcher tick can
retry the cheap preflight without consuming an LLM attempt.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import ProxyHandler, Request, build_opener

LOG = logging.getLogger("hermes.plugins.post_ai_combat_hardening")
AUDIT = Path("/opt/data/logs/post-ai-combat-hardening/events.jsonl")
_ALLOWED_PROFILES = {"implementation-worker", "implementation-architect"}
_OPENER = build_opener(ProxyHandler({}))
_ORIGINAL_GUARD = None
_LAST_UNAVAILABLE_LOG: dict[str, float] = {}


def _audit(event: str, **payload: Any) -> None:
    try:
        AUDIT.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": int(time.time()), "event": event, **payload}, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _git(workspace: Path, *args: str, timeout: int = 20, text: bool = False):
    return subprocess.run(
        ["git", "-C", str(workspace), *args],
        capture_output=True,
        text=text,
        timeout=timeout,
        check=False,
    )


def _in_head(workspace: Path, rel: str) -> bool:
    proc = _git(workspace, "cat-file", "-e", f"HEAD:{rel}", text=True)
    return proc.returncode == 0


def _clean_transient_uids(workspace: Path) -> dict[str, Any]:
    """Remove only untracked UID files whose sibling .gd is already in HEAD.

    This intentionally preserves:
    - any UID present in HEAD (tracked/canonical),
    - any UID associated with a new/untracked .gd file,
    - symlinks/non-regular files,
    - anything outside the worktree.
    """
    result: dict[str, Any] = {"ok": True, "removed": [], "preserved": []}
    try:
        root = workspace.resolve(strict=False)
        if not root.is_dir():
            return {"ok": False, "error": "workspace_missing", "removed": [], "preserved": []}
        inside = _git(root, "rev-parse", "--is-inside-work-tree", text=True)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            return {"ok": False, "error": "not_git_worktree", "removed": [], "preserved": []}
        proc = _git(root, "ls-files", "--others", "--exclude-standard", "-z", "--", "*.gd.uid")
        if proc.returncode != 0:
            return {"ok": False, "error": "git_ls_files_failed", "removed": [], "preserved": []}
        raw = proc.stdout or b""
        for item in raw.split(b"\0"):
            if not item:
                continue
            rel = item.decode("utf-8", "surrogateescape")
            if not rel.endswith(".gd.uid"):
                continue
            uid_path = (root / rel).resolve(strict=False)
            try:
                uid_path.relative_to(root)
            except ValueError:
                result["preserved"].append({"path": rel, "reason": "escaped_root"})
                continue
            if uid_path.is_symlink() or not uid_path.is_file():
                result["preserved"].append({"path": rel, "reason": "not_regular"})
                continue
            gd_rel = rel[:-4]  # remove trailing '.uid' -> '.gd'
            if _in_head(root, rel):
                result["preserved"].append({"path": rel, "reason": "uid_in_HEAD"})
                continue
            if not _in_head(root, gd_rel):
                result["preserved"].append({"path": rel, "reason": "sibling_gd_not_in_HEAD"})
                continue
            uid_path.unlink()
            result["removed"].append(rel)
        if result["removed"]:
            _audit("transient_uid_cleanup", workspace=str(root), removed=result["removed"][:200], count=len(result["removed"]))
        return result
    except Exception as exc:
        result["ok"] = False
        result["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        return result


def _task_requires_gwrm(body: str) -> bool:
    return bool(re.search(r"(?mi)^\s*gwrm_required\s*:\s*true\s*(?:#.*)?$", body or ""))


def _gwrm_connection() -> tuple[str, str]:
    test_url = str(os.environ.get("HERMES_GWRM_PREFLIGHT_TEST_URL") or "").strip()
    test_key = str(os.environ.get("HERMES_GWRM_PREFLIGHT_TEST_KEY") or "").strip()
    if test_url:
        return test_url.rstrip("/"), test_key or "smoke-test"

    from hermes_cli.config import load_config_readonly
    cfg = load_config_readonly() or {}
    env = (((cfg.get("lsp") or {}).get("servers") or {}).get("godot-gdscript") or {}).get("env") or {}
    url = str(env.get("GWRM_CONTROL_URL") or "").strip().rstrip("/")
    key = str(env.get("GWRM_API_KEY") or "").strip()
    if not url or not key:
        raise RuntimeError("GWRM control URL/API key missing from godot-gdscript env")
    return url, key


def _gwrm_health(timeout: float = 2.0) -> tuple[bool, str]:
    try:
        url, key = _gwrm_connection()
        request = Request(
            f"{url}/api/v1/worktrees",
            method="GET",
            headers={"X-API-Key": key, "Accept": "application/json"},
        )
        with _OPENER.open(request, timeout=timeout) as response:
            if int(getattr(response, "status", 200)) >= 400:
                return False, f"http_{response.status}"
            raw = response.read()
        if raw:
            json.loads(raw.decode("utf-8"))
        return True, "ok"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:500]


def _inspect_task(conn, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,status,assignee,workspace_kind,workspace_path,body FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {name: row[name] for name in row.keys()}


def _guarded_respawn(conn, task_id: str, *args, **kwargs):
    # Clean only proven transient UIDs before any downstream retry fingerprint.
    try:
        task = _inspect_task(conn, task_id)
        if task and str(task.get("workspace_kind") or "") == "worktree":
            workspace = str(task.get("workspace_path") or "").strip()
            if workspace:
                _clean_transient_uids(Path(workspace))
    except Exception:
        task = None

    # Preserve every pre-existing/native/custom respawn guard.
    reason = _ORIGINAL_GUARD(conn, task_id, *args, **kwargs)
    if reason is not None:
        return reason

    lane = kwargs.get("lane", "ready")
    if lane != "ready":
        return None
    if task is None:
        task = _inspect_task(conn, task_id)
    if not task:
        return None
    if str(task.get("assignee") or "") not in _ALLOWED_PROFILES:
        return None
    if str(task.get("workspace_kind") or "") != "worktree":
        return None
    if not _task_requires_gwrm(str(task.get("body") or "")):
        return None

    healthy, detail = _gwrm_health()
    if healthy:
        return None

    now = time.monotonic()
    last = _LAST_UNAVAILABLE_LOG.get(task_id, 0.0)
    if now - last >= 60:
        _LAST_UNAVAILABLE_LOG[task_id] = now
        _audit("gwrm_preflight_hold", task_id=task_id, detail=detail)
    # Returning a reason prevents claim/spawn for this dispatcher tick only.
    # The task stays READY; no LLM attempt is consumed.
    return "gwrm_preflight_unavailable"


_guarded_respawn._post_ai_combat_guard = True


def _pre_tool_call(tool_name=None, **kwargs):
    del kwargs
    name = str(tool_name or "")
    if not name.endswith("worktree_guardian_verify"):
        return None
    workspace = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if workspace:
        result = _clean_transient_uids(Path(workspace))
        if not result.get("ok"):
            _audit("transient_uid_cleanup_failed", workspace=workspace, error=result.get("error"))
    return None


def register(ctx):
    global _ORIGINAL_GUARD
    from hermes_cli import kanban_db as kb

    # HERMES_V021_DISPATCH_GUARD_2026_09_07
    import importlib
    kbd = importlib.import_module("hermes_cli.kanban_db_dispatch")
    current = kbd.check_respawn_guard
    if not getattr(current, "_post_ai_combat_guard", False):
        _ORIGINAL_GUARD = current
        _guarded_respawn._post_ai_combat_original = current
        kbd.check_respawn_guard = _guarded_respawn
        kb.check_respawn_guard = _guarded_respawn
        _audit("respawn_guard_patched")
    else:
        _ORIGINAL_GUARD = getattr(current, "_post_ai_combat_original", None) or _ORIGINAL_GUARD

    ctx.register_hook("pre_tool_call", _pre_tool_call)
