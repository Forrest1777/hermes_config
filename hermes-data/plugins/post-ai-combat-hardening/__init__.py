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
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

LOG = logging.getLogger("hermes.plugins.post_ai_combat_hardening")
AUDIT = Path("/opt/data/logs/post-ai-combat-hardening/events.jsonl")
_ALLOWED_PROFILES = {"implementation-worker", "implementation-architect"}
_OPENER = build_opener(ProxyHandler({}))
_ORIGINAL_GUARD = None
_LAST_UNAVAILABLE_LOG: dict[tuple[str, str], float] = {}
_GWRM_ENV_REF_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_GWRM_AUDIT_INTERVAL_SECONDS = float(
    os.environ.get(
        "HERMES_GWRM_PREFLIGHT_AUDIT_INTERVAL_SECONDS",
        "300",
    )
)
# HERMES_GWRM_DIAGNOSTIC_HARDENING_2026_09_14


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


def _resolve_gwrm_env_value(value: Any) -> tuple[str, str | None]:
    raw = str(value or "").strip()
    match = _GWRM_ENV_REF_RE.fullmatch(raw)
    if not match:
        return raw, None

    env_name = match.group(1)
    return str(os.environ.get(env_name) or "").strip(), env_name


def _connection_from_config(cfg: dict[str, Any]) -> tuple[str, str]:
    """Resolve GWRM connection using process env as the canonical secret source.

    The YAML carries only ${GWRM_API_KEY}; the actual secret must come from
    the Hermes container environment.  This avoids divergence between config
    loader interpolation and the service environment used by the control plane.
    """
    # HERMES_GWRM_AUTH_ENV_CANONICAL_2026_09_14
    lsp = cfg.get("lsp") or {}
    servers = lsp.get("servers") or {}
    godot = servers.get("godot-gdscript") or {}
    env = godot.get("env") or {}

    raw_url = str(env.get("GWRM_CONTROL_URL") or "").strip()
    raw_key = str(env.get("GWRM_API_KEY") or "").strip()

    if not raw_url:
        raise RuntimeError(
            "gwrm_control_url_missing: "
            "godot-gdscript.env.GWRM_CONTROL_URL"
        )

    if not raw_key:
        raise RuntimeError(
            "gwrm_api_key_config_missing: "
            "godot-gdscript.env.GWRM_API_KEY"
        )

    url, url_env = _resolve_gwrm_env_value(raw_url)

    if url_env and not url:
        raise RuntimeError(
            f"gwrm_control_url_env_missing: {url_env}"
        )

    if not url:
        raise RuntimeError("gwrm_control_url_empty")

    # load_config_readonly() may already resolve ${GWRM_API_KEY} before this
    # plugin sees the config.  Canonical-placeholder enforcement belongs to
    # hermes-config-preflight.py, which validates the raw YAML on disk.
    #
    # Runtime authentication always uses the process environment as the
    # canonical secret source, regardless of whether raw_key is still the
    # placeholder or already the resolved value.
    key = str(os.environ.get("GWRM_API_KEY") or "").strip()
    if not key:
        raise RuntimeError(
            "gwrm_api_key_env_missing: GWRM_API_KEY"
        )

    return url.rstrip("/"), key


def _gwrm_connection() -> tuple[str, str]:
    test_url = str(
        os.environ.get("HERMES_GWRM_PREFLIGHT_TEST_URL")
        or ""
    ).strip()
    test_key = str(
        os.environ.get("HERMES_GWRM_PREFLIGHT_TEST_KEY")
        or ""
    ).strip()

    if test_url:
        return test_url.rstrip("/"), test_key or "smoke-test"

    from hermes_cli.config import load_config_readonly

    cfg = load_config_readonly() or {}
    return _connection_from_config(cfg)


def _gwrm_health(timeout: float = 2.0) -> tuple[bool, str]:
    try:
        url, key = _gwrm_connection()
        request = Request(
            f"{url}/api/v1/worktrees",
            method="GET",
            headers={
                "X-API-Key": key,
                "Accept": "application/json",
            },
        )

        with _OPENER.open(
            request,
            timeout=timeout,
        ) as response:
            status = int(
                getattr(response, "status", 200)
            )

            if status >= 400:
                return False, f"http_{status}"

            raw = response.read()

        if raw:
            json.loads(raw.decode("utf-8"))

        return True, "ok"

    except HTTPError as exc:
        if exc.code == 401:
            return False, "http_401_unauthorized"
        if exc.code == 403:
            return False, "http_403_forbidden"
        return False, f"http_{exc.code}"

    except URLError as exc:
        return False, (
            f"network_error: {exc.reason}"
        )[:500]

    except Exception as exc:
        return False, (
            f"{type(exc).__name__}: {exc}"
        )[:500]


def _inspect_task(conn, task_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id,status,assignee,workspace_kind,workspace_path,body FROM tasks WHERE id=?",
        (task_id,),
    ).fetchone()
    if row is None:
        return None
    return {name: row[name] for name in row.keys()}


def _guarded_respawn(conn, task_id: str, *args, **kwargs):
    # HERMES_PRESERVE_DIRTY_UID_CHECKPOINT_2026_09_09
    # Retry guards must be observational: never mutate a dirty worktree before
    # the native/custom retry checkpoint logic captures and classifies it.
    task = None
    try:
        task = _inspect_task(conn, task_id)
        if task and str(task.get("workspace_kind") or "") == "worktree":
            workspace = str(task.get("workspace_path") or "").strip()
            if workspace:
                _audit(
                    "transient_uid_cleanup_skipped_preserve_checkpoint",
                    task_id=task_id,
                    workspace=workspace,
                    phase="pre_spawn_retry_guard",
                )
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
    audit_key = (task_id, detail)
    last = _LAST_UNAVAILABLE_LOG.get(audit_key, 0.0)
    if now - last >= _GWRM_AUDIT_INTERVAL_SECONDS:
        _LAST_UNAVAILABLE_LOG[audit_key] = now
        _audit(
            "gwrm_preflight_hold",
            task_id=task_id,
            reason_code=detail.split(":", 1)[0],
            detail=detail,
        )
    # Returning a reason prevents claim/spawn for this dispatcher tick only.
    # The task stays READY; no LLM attempt is consumed.
    return "gwrm_preflight_unavailable"


_guarded_respawn._post_ai_combat_guard = True


def _pre_tool_call(tool_name=None, **kwargs):
    del kwargs
    name = str(tool_name or "")
    if not name.endswith("worktree_guardian_verify"):
        return None

    # HERMES_PRESERVE_DIRTY_UID_CHECKPOINT_2026_09_09
    # Verification must never normalize/mutate the candidate checkpoint.
    workspace = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if workspace:
        _audit(
            "transient_uid_cleanup_skipped_preserve_checkpoint",
            task_id=str(os.environ.get("HERMES_KANBAN_TASK") or "").strip() or None,
            workspace=workspace,
            phase="pre_worktree_guardian_verify",
        )
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
