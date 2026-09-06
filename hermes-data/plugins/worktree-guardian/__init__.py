"""Worktree lifecycle guardian for orchestrated Kanban implementation.

This plugin keeps worktree provisioning outside Hermes core. The orchestrator
prepares a child card held behind a deliberately non-spawnable assignee fence,
validates it, and only then assigns the real worker/architect profile so normal
dispatch can begin. Worker/architect profiles only receive a read-only
verification surface. A narrow stale index.lock recovery surface replaces the
legacy standalone helper.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

TOOLSET = "worktree_guardian"
PREPARE_PROFILES = {"implementation-orchestrator"}
VERIFY_PROFILES = {"implementation-worker", "implementation-architect"}
ALLOWED_TARGET_PROFILES = VERIFY_PROFILES

PREPARE_SCHEMA = {
    "name": "worktree_guardian_prepare",
    "description": (
        "Prepare and validate an implementation-worker/implementation-architect worktree "
        "while the card is fenced behind a non-spawnable holding assignee. Retries Git-aware "
        "provisioning with configurable timeouts and assigns the real target profile only "
        "after validation passes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_task_id": {"type": "string", "description": "Held child card id."},
            "target_profile": {
                "type": "string",
                "enum": ["implementation-worker", "implementation-architect"],
                "description": "Real assignee to activate only after worktree validation passes.",
            },
            "base_ref": {
                "type": "string",
                "description": "Expected immutable base ref/commit. If omitted, the plugin tries to read base_ref from the card body.",
            },
            "repo_root": {
                "type": "string",
                "description": "Repository root. Optional when workspace_path is <repo>/.worktrees/<card>.",
            },
            "reason": {
                "type": "string",
                "description": "Short orchestration audit reason.",
            },
        },
        "required": ["target_task_id", "target_profile"],
        "additionalProperties": False,
    },
}

VERIFY_SCHEMA = {
    "name": "worktree_guardian_verify",
    "description": (
        "Fail-closed verification of the current worker/architect worktree before any "
        "technical read, edit, test, validation or commit. This tool never repairs the worktree."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "base_ref": {
                "type": "string",
                "description": "Expected base ref/commit. If omitted, the plugin tries to read base_ref from the current card body.",
            }
        },
        "additionalProperties": False,
    },
}

RECOVER_SCHEMA = {
    "name": "worktree_guardian_recover_index_lock",
    "description": (
        "Orchestrator-only conservative stale index.lock recovery for a related blocked child card. "
        "Removes only the exact Git-resolved zero-byte lock when old enough and no associated Git process exists."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_task_id": {"type": "string", "description": "Related child card id."},
            "reason": {"type": "string", "description": "Short audit reason."},
        },
        "required": ["target_task_id", "reason"],
        "additionalProperties": False,
    },
}


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False)


def _profile() -> str:
    return str(os.environ.get("HERMES_PROFILE") or "")


def _prepare_available() -> bool:
    return _profile() in PREPARE_PROFILES and bool(os.environ.get("HERMES_KANBAN_TASK"))


def _verify_available() -> bool:
    return (
        _profile() in VERIFY_PROFILES
        and bool(os.environ.get("HERMES_KANBAN_TASK"))
        and bool(os.environ.get("HERMES_KANBAN_WORKSPACE"))
    )


def _cfg() -> dict[str, Any]:
    defaults = {
        "workspace_root": "/workspace",
        "holding_assignee": "__worktree_guardian_hold__",
        "required_files": ["project.godot", "AGENTS.md"],
        "validation_timeout_seconds": 60,
        "provisioning": {
            "max_attempts": 3,
            "timeout_initial_seconds": 120,
            "timeout_increment_seconds": 90,
            "timeout_max_seconds": 300,
            "recovery_command_timeout_seconds": 90,
            "process_settle_seconds": 1,
        },
        "stale_index_lock": {"min_age_seconds": 120},
    }
    try:
        from hermes_cli.config import load_config

        raw = load_config().get("worktree_guardian", {}) or {}
        if isinstance(raw, dict):
            for key in ("workspace_root", "holding_assignee", "required_files", "validation_timeout_seconds"):
                if key in raw:
                    defaults[key] = raw[key]
            for section in ("provisioning", "stale_index_lock"):
                incoming = raw.get(section)
                if isinstance(incoming, dict):
                    defaults[section].update(incoming)
    except Exception:
        pass
    return defaults


def _audit(payload: dict[str, Any]) -> None:
    try:
        path = Path("/opt/data/logs/worktree-guardian/events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": int(time.time()), "profile": _profile(), **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _run(cmd: list[str], timeout: int = 60, *, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        capture_output=True,
        text=text,
        timeout=max(1, int(timeout)),
        check=False,
    )


def _run_git(cwd: Path, args: list[str], timeout: int, *, text: bool = True) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(cwd), *args], timeout=timeout, text=text)


def _run_mutating_process_group(cmd: list[str], timeout: int) -> dict[str, Any]:
    """Run one mutating Git command with a timeout that terminates descendants too."""
    started = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    timed_out = False
    try:
        out, err = proc.communicate(timeout=max(1, int(timeout)))
    except subprocess.TimeoutExpired:
        timed_out = True
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass
        try:
            out, err = proc.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            out, err = proc.communicate()
    return {
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "stdout": (out or "")[-4000:],
        "stderr": (err or "")[-4000:],
        "elapsed_seconds": round(time.monotonic() - started, 3),
    }


def _active_git_process(worktree: Path, common_git: Path, repo_root: Optional[Path] = None) -> Optional[dict[str, Any]]:
    proc_root = Path("/proc")
    if not proc_root.exists():
        return None
    me = os.getpid()
    refs = [str(worktree), str(common_git)]
    if repo_root is not None:
        refs.append(str(repo_root))
    for item in proc_root.iterdir():
        if not item.name.isdigit() or int(item.name) == me:
            continue
        try:
            raw = (item / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
            if "git" not in raw.lower():
                continue
            cwd = (item / "cwd").resolve()
            if _under(cwd, worktree) or _under(cwd, common_git) or any(ref in raw for ref in refs):
                return {"pid": int(item.name), "cmdline": raw[:700], "cwd": str(cwd)}
        except Exception:
            continue
    return None


def _parse_worktree_block(repo_root: Path, target: Path, timeout: int) -> Optional[dict[str, Any]]:
    p = _run_git(repo_root, ["worktree", "list", "--porcelain"], timeout)
    if p.returncode != 0:
        return None
    wanted = str(target.resolve(strict=False))
    for block in p.stdout.strip().split("\n\n") if p.stdout.strip() else []:
        lines = block.splitlines()
        if not lines or not lines[0].startswith("worktree "):
            continue
        raw_path = lines[0][len("worktree "):].strip()
        try:
            actual = str(Path(raw_path).resolve(strict=False))
        except Exception:
            actual = raw_path
        if actual != wanted:
            continue
        info: dict[str, Any] = {"raw": block, "path": actual, "locked": False, "lock_reason": None}
        for line in lines[1:]:
            if line.startswith("HEAD "):
                info["head"] = line[5:].strip()
            elif line.startswith("branch "):
                info["branch_ref"] = line[7:].strip()
            elif line.startswith("locked"):
                info["locked"] = True
                info["lock_reason"] = line[len("locked"):].strip() or None
            elif line == "prunable":
                info["prunable"] = True
        return info
    return None


def _repo_common(repo_root: Path, timeout: int) -> Optional[Path]:
    p = _run_git(repo_root, ["rev-parse", "--path-format=absolute", "--git-common-dir"], timeout)
    if p.returncode != 0:
        return None
    try:
        return Path(p.stdout.strip()).resolve()
    except Exception:
        return None


def _extract_base_ref(body: str) -> Optional[str]:
    patterns = (
        r'(?mi)^\s*base_ref\s*:\s*["\']?([^\s"\']+)',
        r'["\']base_ref["\']\s*:\s*["\']([^"\']+)["\']',
    )
    for pattern in patterns:
        m = re.search(pattern, body or "")
        if m:
            return m.group(1).strip()
    return None


def _related(conn: Any, root_id: str, target_id: str, target_body: str) -> bool:
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
        rf'[\"\']logical_parent_card_id[\"\']\s*:\s*[\"\']{root_re}[\"\']',
    )
    return any(re.search(p, target_body or "") for p in patterns)


def _infer_repo_root(workspace: Path, explicit: Optional[str], workspace_root: Path) -> Path:
    if explicit:
        repo = Path(explicit).resolve()
    elif workspace.parent.name == ".worktrees":
        repo = workspace.parent.parent.resolve()
    else:
        raise RuntimeError("repo_root required: target workspace_path is not <repo>/.worktrees/<card>")
    if not _under(repo, workspace_root):
        raise RuntimeError("repo_root must resolve under configured workspace_root")
    return repo


def _resolve_commit(repo_root: Path, ref: str, timeout: int) -> str:
    p = _run_git(repo_root, ["rev-parse", "--verify", f"{ref}^{{commit}}"], timeout)
    if p.returncode != 0:
        raise RuntimeError(f"base_ref cannot be resolved in repository: {ref}: {(p.stderr or p.stdout).strip()[-500:]}")
    return p.stdout.strip()


def _branch_tip(repo_root: Path, branch: str, timeout: int) -> Optional[str]:
    p = _run_git(repo_root, ["show-ref", "--verify", "--hash", f"refs/heads/{branch}"], timeout)
    return p.stdout.strip() if p.returncode == 0 else None


# HERMES_OPERATIONAL_HARDENING_2026_09_03: read-only validation of a governance-authorized dirty retry.
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


def _authorized_retry_checkpoint(workspace: Path, head: str, status_sha256: str) -> dict[str, Any] | None:
    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    if not task_id:
        return None
    try:
        state = json.loads(_governor_state_path().read_text(encoding="utf-8"))
        task = (state.get("tasks") or {}).get(task_id) or {}
        auth = task.get("authorized_recovery_checkpoint")
        if not isinstance(auth, dict):
            return None
        auth_state = str(auth.get("state") or "")
        if auth_state not in {"AUTHORIZED", "IN_USE"}:
            return None
        if int(auth.get("expires_at") or 0) < int(time.time()):
            return None
        if str(Path(str(auth.get("workspace") or "")).resolve(strict=False)) != str(workspace.resolve(strict=False)):
            return None
        if str(auth.get("head") or "") != head:
            return None
        if str(auth.get("status_sha256") or "") != status_sha256:
            return None
        if auth_state == "IN_USE":
            current_run = str(os.environ.get("HERMES_KANBAN_RUN_ID") or "")
            if current_run and str(auth.get("run_id") or "") not in {"", current_run}:
                return None
        return auth
    except Exception:
        return None


def _validate_worktree(
    workspace: Path,
    *,
    repo_root: Optional[Path],
    expected_branch: Optional[str],
    base_ref: Optional[str],
    cfg: dict[str, Any],
    require_exact_base: bool = False,
) -> dict[str, Any]:
    timeout = int(cfg.get("validation_timeout_seconds", 60))
    root = Path(str(cfg.get("workspace_root", "/workspace"))).resolve()
    result: dict[str, Any] = {
        "passed": False,
        "workspace": str(workspace.resolve(strict=False)),
        "errors": [],
        "missing_tracked_files": [],
        "skip_worktree_entries": [],
        "required_files": {},
    }
    try:
        real = workspace.resolve(strict=False)
        result["workspace"] = str(real)
        if not _under(real, root):
            raise RuntimeError("workspace outside configured workspace_root")
        if not real.is_dir():
            raise RuntimeError("workspace directory does not exist")

        inside = _run_git(real, ["rev-parse", "--is-inside-work-tree"], timeout)
        if inside.returncode != 0 or inside.stdout.strip() != "true":
            raise RuntimeError("not a valid Git worktree")
        top = _run_git(real, ["rev-parse", "--show-toplevel"], timeout)
        common = _run_git(real, ["rev-parse", "--path-format=absolute", "--git-common-dir"], timeout)
        git_dir = _run_git(real, ["rev-parse", "--absolute-git-dir"], timeout)
        branch = _run_git(real, ["branch", "--show-current"], timeout)
        head = _run_git(real, ["rev-parse", "HEAD"], timeout)
        status = _run_git(real, ["status", "--porcelain=v1", "--untracked-files=all"], timeout)
        for name, proc in (("show-toplevel", top), ("git-common-dir", common), ("git-dir", git_dir), ("branch", branch), ("HEAD", head), ("status", status)):
            if proc.returncode != 0:
                raise RuntimeError(f"git {name} failed: {(proc.stderr or proc.stdout).strip()[-500:]}")

        top_path = Path(top.stdout.strip()).resolve()
        common_path = Path(common.stdout.strip()).resolve()
        git_dir_path = Path(git_dir.stdout.strip()).resolve()
        branch_name = branch.stdout.strip()
        head_sha = head.stdout.strip()
        linked = git_dir_path != common_path
        result.update(
            {
                "inside_worktree": True,
                "linked_worktree": linked,
                "git_toplevel": str(top_path),
                "git_dir": str(git_dir_path),
                "git_common_dir": str(common_path),
                "branch_expected": expected_branch or None,
                "branch_actual": branch_name,
                "head": head_sha,
                "base_ref": base_ref or None,
                "initial_git_clean": not bool(status.stdout.strip()),
                "git_status_sha256": hashlib.sha256((status.stdout or "").encode("utf-8")).hexdigest(),
                "authorized_retry_checkpoint": False,
                "resume_epoch": None,
            }
        )

        if top_path != real:
            result["errors"].append("git toplevel differs from workspace")
        if not linked:
            result["errors"].append("workspace is not a linked worktree")
        if not branch_name:
            result["errors"].append("detached HEAD or missing branch")
        if expected_branch and branch_name != expected_branch:
            result["errors"].append("branch mismatch")
        if status.stdout.strip():
            authorization = _authorized_retry_checkpoint(
                real,
                head_sha,
                result["git_status_sha256"],
            )
            if authorization is not None:
                result["authorized_retry_checkpoint"] = True
                result["resume_epoch"] = authorization.get("resume_epoch")
                result["retry_checkpoint_reason"] = authorization.get("reason")
            else:
                result["errors"].append("worktree is not clean")

        if repo_root is not None:
            repo_common = _repo_common(repo_root, timeout)
            result["repo_root"] = str(repo_root)
            result["repo_common_dir"] = str(repo_common) if repo_common else None
            if repo_common is None or repo_common != common_path:
                result["errors"].append("git common dir differs from expected repository")
            wt_block = _parse_worktree_block(repo_root, real, timeout)
            result["worktree_registered"] = wt_block is not None
            result["worktree_locked"] = bool(wt_block and wt_block.get("locked"))
            result["worktree_lock_reason"] = wt_block.get("lock_reason") if wt_block else None
            if wt_block is None:
                result["errors"].append("worktree is not registered in git worktree list")
            elif wt_block.get("locked"):
                result["errors"].append(f"worktree is locked: {wt_block.get('lock_reason') or 'unspecified'}")

        sparse_checkout = _run_git(real, ["config", "--bool", "core.sparseCheckout"], timeout)
        sparse_index = _run_git(real, ["config", "--bool", "index.sparse"], timeout)
        result["sparse_checkout"] = sparse_checkout.stdout.strip().lower() == "true"
        result["sparse_index"] = sparse_index.stdout.strip().lower() == "true"
        if result["sparse_checkout"]:
            result["errors"].append("sparse checkout enabled")
        if result["sparse_index"]:
            result["errors"].append("sparse index enabled")

        ls_v = _run_git(real, ["ls-files", "-v"], timeout)
        if ls_v.returncode != 0:
            raise RuntimeError(f"git ls-files -v failed: {(ls_v.stderr or ls_v.stdout).strip()[-500:]}")
        result["skip_worktree_entries"] = [line[2:] for line in ls_v.stdout.splitlines() if line.startswith("S ")][:100]
        if result["skip_worktree_entries"]:
            result["errors"].append("SKIP_WORKTREE entries present")

        ls_z = _run_git(real, ["ls-files", "-z"], timeout, text=False)
        if ls_z.returncode != 0:
            stderr = (ls_z.stderr or b"").decode("utf-8", "replace") if isinstance(ls_z.stderr, bytes) else str(ls_z.stderr or "")
            raise RuntimeError(f"git ls-files -z failed: {stderr[-500:]}")
        raw = ls_z.stdout or b""
        tracked = [x.decode("utf-8", "surrogateescape") for x in raw.split(b"\0") if x]
        result["tracked_file_count"] = len(tracked)
        if not tracked:
            result["errors"].append("tracked file index is empty")
        missing: list[str] = []
        for rel in tracked:
            p = real / rel
            if not (p.exists() or p.is_symlink()):
                missing.append(rel)
                if len(missing) >= 200:
                    break
        result["missing_tracked_files"] = missing
        if missing:
            result["errors"].append("tracked files missing from working tree")

        index_q = _run_git(real, ["rev-parse", "--path-format=absolute", "--git-path", "index"], timeout)
        lock_q = _run_git(real, ["rev-parse", "--path-format=absolute", "--git-path", "index.lock"], timeout)
        if index_q.returncode == 0:
            index_path = Path(index_q.stdout.strip())
            result["index_path"] = str(index_path)
            result["index_exists"] = index_path.is_file()
            result["index_size"] = index_path.stat().st_size if index_path.is_file() else None
            if not index_path.is_file() or index_path.stat().st_size <= 0:
                result["errors"].append("Git index is missing or empty")
        else:
            result["index_exists"] = False
            result["index_size"] = None
            result["errors"].append("cannot resolve Git index path")

        if lock_q.returncode == 0:
            lock_path = Path(lock_q.stdout.strip())
            result["index_lock_path"] = str(lock_path)
            result["index_lock_exists"] = lock_path.exists()
            if lock_path.exists():
                try:
                    st = lock_path.stat()
                    result["index_lock_size"] = st.st_size
                    result["index_lock_age_seconds"] = max(0, int(time.time() - st.st_mtime))
                except Exception:
                    pass
                result["errors"].append("index.lock present")

        required = cfg.get("required_files") or []
        for rel in required:
            present = (real / str(rel)).is_file()
            result["required_files"][str(rel)] = present
            if not present:
                result["errors"].append(f"required file missing: {rel}")

        if base_ref:
            base_sha = _resolve_commit(real, base_ref, timeout)
            result["base_commit"] = base_sha
            if require_exact_base:
                result["head_matches_base_ref"] = head_sha == base_sha
                result["head_contains_base_ref"] = head_sha == base_sha
                if head_sha != base_sha:
                    result["errors"].append("HEAD does not exactly match base_ref for fresh provisioning")
            else:
                anc = _run_git(real, ["merge-base", "--is-ancestor", base_sha, "HEAD"], timeout)
                result["head_contains_base_ref"] = anc.returncode == 0
                result["head_matches_base_ref"] = head_sha == base_sha
                if anc.returncode != 0:
                    result["errors"].append("HEAD does not contain base_ref")
        else:
            result["head_contains_base_ref"] = None
            result["head_matches_base_ref"] = None

        result["passed"] = not result["errors"]
    except subprocess.TimeoutExpired as exc:
        result["errors"].append(f"validation command timed out: {exc}")
    except Exception as exc:
        result["errors"].append(str(exc))
    return result


def _recover_stale_index_lock(worktree: Path, cfg: dict[str, Any], *, repo_root: Optional[Path] = None) -> dict[str, Any]:
    timeout = int(cfg.get("validation_timeout_seconds", 60))
    root = Path(str(cfg.get("workspace_root", "/workspace"))).resolve()
    worktree = worktree.resolve(strict=False)
    if not _under(worktree, root):
        return {"ok": False, "error": "worktree outside configured workspace_root"}
    if not worktree.is_dir():
        return {"ok": False, "error": "worktree directory does not exist"}

    chk = _run_git(worktree, ["rev-parse", "--is-inside-work-tree"], timeout)
    if chk.returncode != 0 or chk.stdout.strip() != "true":
        return {"ok": False, "error": "not a valid Git worktree"}
    common_q = _run_git(worktree, ["rev-parse", "--path-format=absolute", "--git-common-dir"], timeout)
    lock_q = _run_git(worktree, ["rev-parse", "--path-format=absolute", "--git-path", "index.lock"], timeout)
    if common_q.returncode != 0 or lock_q.returncode != 0:
        return {"ok": False, "error": "cannot resolve git common dir/index.lock"}
    common = Path(common_q.stdout.strip()).resolve()
    lock = Path(lock_q.stdout.strip()).absolute()
    if lock.name != "index.lock":
        return {"ok": False, "error": "resolved lock has unexpected filename", "lock": str(lock)}
    if lock.is_symlink():
        return {"ok": False, "error": "index.lock is symlink; refusing", "lock": str(lock)}
    resolved_lock = lock.resolve(strict=False)
    if not _under(resolved_lock, common):
        return {"ok": False, "error": "resolved lock escaped repository git dir", "lock": str(lock)}
    if not lock.exists():
        return {"ok": True, "changed": False, "reason": "index.lock absent", "lock": str(lock)}
    if not lock.is_file():
        return {"ok": False, "error": "index.lock is not a regular file", "lock": str(lock)}
    st = lock.stat()
    age = max(0, int(time.time() - st.st_mtime))
    min_age = max(1, int((cfg.get("stale_index_lock") or {}).get("min_age_seconds", 120)))
    if st.st_size != 0:
        return {"ok": False, "error": "index.lock is non-empty; human inspection required", "size": st.st_size, "age_seconds": age}
    if age < min_age:
        return {"ok": False, "error": "index.lock is too recent; refusing stale classification", "size": 0, "age_seconds": age, "minimum_age_seconds": min_age}
    active = _active_git_process(worktree, common, repo_root)
    if active:
        return {"ok": False, "error": "active Git process associated with repository", "process": active}

    attempt = {
        "event": "stale_index_lock_recovery_attempt",
        "worktree": str(worktree),
        "common_git": str(common),
        "lock": str(lock),
        "size": st.st_size,
        "age_seconds": age,
    }
    _audit(attempt)
    lock.unlink()
    status = _run_git(worktree, ["status", "--porcelain=v1", "--untracked-files=all"], timeout)
    result = {
        "ok": status.returncode == 0,
        "changed": True,
        "event": "stale_index_lock_removed",
        "worktree": str(worktree),
        "lock": str(lock),
        "git_status_exit": status.returncode,
        "git_status_stderr": (status.stderr or "")[-500:],
    }
    _audit(result)
    return result


def _cleanup_partial_worktree(repo_root: Path, target: Path, cfg: dict[str, Any]) -> dict[str, Any]:
    """Git-aware cleanup only. Never recursively removes an unregistered directory."""
    prov = cfg.get("provisioning") or {}
    timeout = int(prov.get("recovery_command_timeout_seconds", 90))
    validation_timeout = int(cfg.get("validation_timeout_seconds", 60))
    common = _repo_common(repo_root, validation_timeout)
    if common is None:
        return {"ok": False, "error": "cannot resolve repository common dir"}
    active = _active_git_process(target, common, repo_root)
    if active:
        return {"ok": False, "error": "active Git process prevents cleanup", "process": active}
    block = _parse_worktree_block(repo_root, target, validation_timeout)
    if block is None:
        if target.exists():
            return {
                "ok": False,
                "error": "target directory exists but is not a registered worktree; refusing non-Git deletion",
                "target": str(target),
            }
        prune = _run_git(repo_root, ["worktree", "prune"], timeout)
        return {"ok": prune.returncode == 0, "changed": False, "prune_rc": prune.returncode}

    unlock = _run_mutating_process_group(["git", "-C", str(repo_root), "worktree", "unlock", str(target)], timeout)
    remove = _run_mutating_process_group(["git", "-C", str(repo_root), "worktree", "remove", "--force", str(target)], timeout)
    prune = _run_mutating_process_group(["git", "-C", str(repo_root), "worktree", "prune"], timeout)
    ok = (not remove["timed_out"] and remove["returncode"] == 0 and not target.exists())
    return {
        "ok": ok,
        "changed": True,
        "unlock": unlock,
        "remove": remove,
        "prune": prune,
        "target_exists_after": target.exists(),
    }


def _attempt_timeout(cfg: dict[str, Any], attempt: int) -> int:
    prov = cfg.get("provisioning") or {}
    initial = max(1, int(prov.get("timeout_initial_seconds", 120)))
    increment = max(0, int(prov.get("timeout_increment_seconds", 90)))
    maximum = max(initial, int(prov.get("timeout_max_seconds", 300)))
    return min(initial + max(0, attempt - 1) * increment, maximum)



def _holding_assignee_is_nonspawnable(name: str) -> tuple[bool, Optional[str]]:
    """Prove that the configured holding assignee does not resolve to a real profile."""
    if not name or not name.strip():
        return False, "holding_assignee is empty"
    try:
        from hermes_cli.profiles import profile_exists

        if profile_exists(name):
            return False, f"holding_assignee unexpectedly resolves to a real profile: {name}"
        return True, None
    except Exception as exc:
        # The holding assignee is the dispatch safety boundary. If we cannot
        # prove it is non-spawnable, fail closed rather than guessing.
        return False, f"cannot prove holding_assignee is non-spawnable: {type(exc).__name__}: {exc}"


def _release_prepared_target(
    kb: Any,
    conn: Any,
    *,
    target_id: str,
    target_profile: str,
    holding_assignee: str,
    board: Optional[str],
) -> dict[str, Any]:
    """Remove the assignee fence only after provisioning has already passed.

    Safety property: any race after ``assign_task`` is acceptable because the
    worktree has already been fully validated. Before this function is called,
    the real target profile is never assigned, so dispatcher ticks cannot spawn
    the worker/architect even if Hermes auto-promotes the card's status.
    """
    fresh = kb.get_task(conn, target_id)
    if fresh is None:
        return {"ok": False, "error": "target disappeared before release"}
    if str(getattr(fresh, "assignee", "") or "") != holding_assignee:
        return {
            "ok": False,
            "error": "holding assignee changed before release",
            "assignee": str(getattr(fresh, "assignee", "") or ""),
        }
    if getattr(fresh, "current_run_id", None) is not None or getattr(fresh, "worker_pid", None) is not None:
        return {"ok": False, "error": "target acquired run ownership before release"}
    run_count = int(conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (target_id,)).fetchone()[0])
    if run_count != 0:
        return {"ok": False, "error": "target has prior runs before release", "run_count": run_count}
    status_before = str(getattr(fresh, "status", "") or "")
    if status_before not in {"ready", "blocked", "todo"}:
        return {"ok": False, "error": "target status is not releasable", "status": status_before}

    if not kb.assign_task(conn, target_id, target_profile):
        return {"ok": False, "error": "native assign_task refused activation", "status": status_before}

    after_assign = kb.get_task(conn, target_id)
    status_after_assign = str(getattr(after_assign, "status", "") or "") if after_assign else ""
    native_unblock = None
    if status_after_assign == "blocked":
        native_unblock = bool(kb.unblock_task(conn, target_id))
        after_unblock = kb.get_task(conn, target_id)
        resulting_status = str(getattr(after_unblock, "status", "") or "") if after_unblock else ""
        # A concurrent Hermes recompute may have promoted the card between the
        # assignment and unblock calls. Once provisioning passed that is safe;
        # accept the already-ready/todo state even if unblock_task returns False.
        if not native_unblock and resulting_status not in {"ready", "todo"}:
            return {
                "ok": False,
                "error": "target assigned after validation but native unblock_task refused release",
                "status": resulting_status,
                "activated_profile": target_profile,
                "safe_to_dispatch": True,
            }
    else:
        resulting_status = status_after_assign

    if resulting_status not in {"ready", "todo"}:
        return {
            "ok": False,
            "error": "target activation landed in unexpected status",
            "status": resulting_status,
            "activated_profile": target_profile,
            "safe_to_dispatch": True,
        }
    _notify(kb, conn, target_id, board, ["assignee", "status"])
    return {
        "ok": True,
        "released": True,
        "holding_assignee": holding_assignee,
        "activated_profile": target_profile,
        "status_before": status_before,
        "resulting_status": resulting_status,
        "native_unblock": native_unblock,
    }


def _notify(kb: Any, conn: Any, task_id: str, board: Optional[str], fields: list[str]) -> None:
    try:
        kb.notify_task_updated(conn, task_id, fields, board=board)
    except Exception:
        pass


def _add_comment(kb: Any, conn: Any, task_id: str, body: str) -> None:
    try:
        kb.add_comment(conn, task_id, author="worktree-guardian", body=body)
    except Exception:
        pass


def _prepare_handler(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not _prepare_available():
        return _err("worktree_guardian_prepare unavailable outside dispatched implementation-orchestrator")
    root_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    target_id = str(args.get("target_task_id") or "").strip()
    if not re.fullmatch(r"t_[0-9a-fA-F]+", target_id):
        return _err("invalid target_task_id")
    if target_id == root_id:
        return _err("target must be a child card, not the active root")
    cfg = _cfg()
    board = os.environ.get("HERMES_KANBAN_BOARD") or None
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect(board=board)
        try:
            root = kb.get_task(conn, root_id)
            target = kb.get_task(conn, target_id)
            if root is None or target is None:
                return _err("root or target task not found", root_task_id=root_id, target_task_id=target_id)
            if str(getattr(root, "assignee", "") or "") != "implementation-orchestrator":
                return _err("active root is not assigned to implementation-orchestrator")
            target_profile = str(args.get("target_profile") or "").strip()
            if target_profile not in ALLOWED_TARGET_PROFILES:
                return _err("target_profile is outside guardian allowlist", target_profile=target_profile)
            holding_assignee = str(cfg.get("holding_assignee") or "").strip()
            hold_ok, hold_error = _holding_assignee_is_nonspawnable(holding_assignee)
            if not hold_ok:
                return _err(hold_error or "holding_assignee safety check failed", human_required=True)
            assignee = str(getattr(target, "assignee", "") or "")
            if assignee != holding_assignee:
                return _err(
                    "target is not behind the configured non-spawnable assignee fence; refusing pre-dispatch provisioning",
                    assignee=assignee,
                    expected_holding_assignee=holding_assignee,
                    target_profile=target_profile,
                )
            if not _related(conn, root_id, target_id, str(getattr(target, "body", "") or "")):
                return _err("target is not linked/logically owned by active root")
            status = str(getattr(target, "status", "") or "")
            if status not in {"ready", "blocked", "todo"}:
                return _err(
                    "held target must be ready/blocked/todo before initial guardian preparation",
                    status=status,
                )
            if getattr(target, "current_run_id", None) is not None or getattr(target, "worker_pid", None) is not None:
                return _err("target already has run ownership; refusing pre-dispatch provisioning")
            run_count = int(conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id = ?", (target_id,)).fetchone()[0])
            if run_count != 0:
                return _err("target has prior runs; destructive reprovisioning is not allowed", run_count=run_count)
            if str(getattr(target, "workspace_kind", "") or "") != "worktree":
                return _err("target workspace_kind must be worktree")
            raw_workspace = str(getattr(target, "workspace_path", "") or "").strip()
            workspace_root = Path(str(cfg.get("workspace_root", "/workspace"))).resolve()
            explicit_repo = str(args.get("repo_root") or "").strip()
            if explicit_repo:
                repo_root = Path(explicit_repo).resolve()
                if not _under(repo_root, workspace_root):
                    return _err("repo_root must resolve under configured workspace_root", repo_root=str(repo_root))
                desired_target = (repo_root / ".worktrees" / target_id).resolve(strict=False)
                if raw_workspace:
                    current_path = Path(raw_workspace).resolve(strict=False)
                    # A board default can leave workspace_path pointing at the main checkout.
                    # While the non-spawnable assignee fence is intact, normalize only that benign shape.
                    if current_path == repo_root:
                        target_path = desired_target
                        kb.set_workspace_path(conn, target_id, str(target_path))
                        setattr(target, "workspace_path", str(target_path))
                        _notify(kb, conn, target_id, board, ["workspace_path"])
                    elif current_path == desired_target:
                        target_path = current_path
                    else:
                        return _err(
                            "target workspace_path conflicts with repo_root; refusing to rewrite arbitrary path",
                            workspace=str(current_path),
                            expected_workspace=str(desired_target),
                        )
                else:
                    target_path = desired_target
                    kb.set_workspace_path(conn, target_id, str(target_path))
                    setattr(target, "workspace_path", str(target_path))
                    _notify(kb, conn, target_id, board, ["workspace_path"])
            else:
                if not raw_workspace:
                    return _err("repo_root is required when target workspace_path is missing")
                target_path = Path(raw_workspace).resolve(strict=False)
                if not _under(target_path, workspace_root):
                    return _err("target workspace_path is outside configured workspace_root", workspace=str(target_path))
                repo_root = _infer_repo_root(target_path, None, workspace_root)
                if target_path.name != target_id:
                    return _err("per-card worktree path must end with target_task_id when repo_root is inferred", workspace=str(target_path))

            repo_top = _run_git(repo_root, ["rev-parse", "--show-toplevel"], int(cfg.get("validation_timeout_seconds", 60)))
            if repo_top.returncode != 0 or Path(repo_top.stdout.strip()).resolve() != repo_root:
                return _err("repo_root is not a valid Git repository root", repo_root=str(repo_root))
            expected_branch = str(getattr(target, "branch_name", "") or "").strip() or f"wt/{target_id}"
            if not str(getattr(target, "branch_name", "") or "").strip():
                kb.set_branch_name(conn, target_id, expected_branch)
                setattr(target, "branch_name", expected_branch)
                _notify(kb, conn, target_id, board, ["branch_name"])
            base_ref = str(args.get("base_ref") or _extract_base_ref(str(getattr(target, "body", "") or "")) or "").strip()
            if not base_ref:
                return _err("base_ref is required and could not be derived from card body")
            base_commit = _resolve_commit(repo_root, base_ref, int(cfg.get("validation_timeout_seconds", 60)))

            existing = _validate_worktree(
                target_path,
                repo_root=repo_root,
                expected_branch=expected_branch,
                base_ref=base_commit,
                cfg=cfg,
                require_exact_base=True,
            ) if target_path.exists() else {"passed": False, "errors": ["workspace absent"]}
            if existing.get("passed"):
                activation = _release_prepared_target(
                    kb,
                    conn,
                    target_id=target_id,
                    target_profile=target_profile,
                    holding_assignee=holding_assignee,
                    board=board,
                )
                if not activation.get("ok"):
                    return _err("worktree valid but assignee-fence release failed", validation=existing, activation=activation, human_required=True)
                _add_comment(kb, conn, target_id, f"WORKTREE_GUARDIAN_PREPARED\nroot_task_id: {root_id}\ntarget_profile: {target_profile}\nholding_assignee: {holding_assignee}\nbase_ref: {base_commit}\nbranch: {expected_branch}\nworkspace: {target_path}\nprovisioning: existing_valid\n")
                payload = {"event": "prepare_existing_valid", "root_task_id": root_id, "target_task_id": target_id, "workspace": str(target_path), "branch": expected_branch, "base_commit": base_commit, "target_profile": target_profile, "activation": activation, "resulting_status": activation.get("resulting_status")}
                _audit(payload)
                return _ok(prepared=True, released=True, validation=existing, **payload)

            # Any existing/registered artifact on a never-run held card is treated as failed provisioning.
            if target_path.exists() or _parse_worktree_block(repo_root, target_path, int(cfg.get("validation_timeout_seconds", 60))):
                cleanup = _cleanup_partial_worktree(repo_root, target_path, cfg)
                _audit({"event": "preexisting_partial_cleanup", "root_task_id": root_id, "target_task_id": target_id, "cleanup": cleanup, "validation": existing})
                if not cleanup.get("ok"):
                    _add_comment(kb, conn, target_id, f"WORKTREE_GUARDIAN_HUMAN_REQUIRED\nroot_task_id: {root_id}\nreason: unable to safely clean preexisting partial worktree\ndetails: {json.dumps(cleanup, ensure_ascii=False)[:3000]}\n")
                    return _err("unable to safely clean preexisting partial worktree", human_required=True, cleanup=cleanup, validation=existing)

            branch_tip = _branch_tip(repo_root, expected_branch, int(cfg.get("validation_timeout_seconds", 60)))
            if branch_tip and branch_tip != base_commit:
                return _err(
                    "existing target branch does not match base_ref; refusing to reset/delete branch",
                    human_required=True,
                    branch=expected_branch,
                    branch_tip=branch_tip,
                    base_commit=base_commit,
                )

            prov = cfg.get("provisioning") or {}
            max_attempts = max(1, int(prov.get("max_attempts", 3)))
            attempts: list[dict[str, Any]] = []
            for attempt in range(1, max_attempts + 1):
                timeout = _attempt_timeout(cfg, attempt)
                current_tip = _branch_tip(repo_root, expected_branch, int(cfg.get("validation_timeout_seconds", 60)))
                if current_tip and current_tip != base_commit:
                    return _err("branch changed during provisioning; refusing", human_required=True, branch_tip=current_tip, base_commit=base_commit, attempts=attempts)
                if current_tip:
                    cmd = ["git", "-C", str(repo_root), "worktree", "add", str(target_path), expected_branch]
                else:
                    cmd = ["git", "-C", str(repo_root), "worktree", "add", "-b", expected_branch, str(target_path), base_commit]
                outcome = _run_mutating_process_group(cmd, timeout)
                record: dict[str, Any] = {"attempt": attempt, "timeout_seconds": timeout, "command_result": outcome}
                settle = max(0, int(prov.get("process_settle_seconds", 1)))
                if settle:
                    time.sleep(settle)
                validation = _validate_worktree(
                    target_path,
                    repo_root=repo_root,
                    expected_branch=expected_branch,
                    base_ref=base_commit,
                    cfg=cfg,
                    require_exact_base=True,
                )
                record["validation"] = validation
                attempts.append(record)
                _audit({"event": "provision_attempt", "root_task_id": root_id, "target_task_id": target_id, **record})
                if not outcome.get("timed_out") and outcome.get("returncode") == 0 and validation.get("passed"):
                    activation = _release_prepared_target(
                        kb,
                        conn,
                        target_id=target_id,
                        target_profile=target_profile,
                        holding_assignee=holding_assignee,
                        board=board,
                    )
                    if not activation.get("ok"):
                        return _err("provisioned worktree passed but assignee-fence release failed", human_required=True, attempts=attempts, validation=validation, activation=activation)
                    _add_comment(kb, conn, target_id, f"WORKTREE_GUARDIAN_PREPARED\nroot_task_id: {root_id}\ntarget_profile: {target_profile}\nholding_assignee: {holding_assignee}\nattempt: {attempt}\ntimeout_seconds: {timeout}\nbase_ref: {base_commit}\nbranch: {expected_branch}\nworkspace: {target_path}\n")
                    payload = {"event": "prepare_success", "root_task_id": root_id, "target_task_id": target_id, "workspace": str(target_path), "branch": expected_branch, "base_commit": base_commit, "target_profile": target_profile, "attempt": attempt, "timeout_seconds": timeout, "activation": activation, "resulting_status": activation.get("resulting_status")}
                    _audit(payload)
                    return _ok(prepared=True, released=True, attempts=attempts, validation=validation, **payload)

                cleanup = _cleanup_partial_worktree(repo_root, target_path, cfg)
                record["cleanup"] = cleanup
                _audit({"event": "provision_attempt_cleanup", "root_task_id": root_id, "target_task_id": target_id, "attempt": attempt, "cleanup": cleanup})
                if not cleanup.get("ok"):
                    _add_comment(kb, conn, target_id, f"WORKTREE_GUARDIAN_HUMAN_REQUIRED\nroot_task_id: {root_id}\nreason: safe cleanup failed after provisioning attempt {attempt}\ndetails: {json.dumps(cleanup, ensure_ascii=False)[:3000]}\n")
                    return _err("safe cleanup failed after provisioning attempt", human_required=True, attempts=attempts)

            _add_comment(kb, conn, target_id, f"WORKTREE_GUARDIAN_HUMAN_REQUIRED\nroot_task_id: {root_id}\nreason: provisioning attempts exhausted\nmax_attempts: {max_attempts}\nlast_attempt: {json.dumps(attempts[-1], ensure_ascii=False)[:5000] if attempts else 'none'}\n")
            payload = {"event": "prepare_exhausted", "root_task_id": root_id, "target_task_id": target_id, "max_attempts": max_attempts, "attempts": attempts}
            _audit(payload)
            return _err("worktree provisioning attempts exhausted; target remains fenced by non-spawnable assignee", human_required=True, prepared=False, released=False, holding_assignee=holding_assignee, attempts=attempts)
        finally:
            conn.close()
    except Exception as exc:
        _audit({"event": "prepare_error", "root_task_id": root_id, "target_task_id": target_id, "error": f"{type(exc).__name__}: {exc}"[:1500]})
        return _err(f"{type(exc).__name__}: {exc}"[:1500])


def _verify_handler(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not _verify_available():
        return _err("worktree_guardian_verify unavailable outside implementation-worker/implementation-architect task context")
    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    workspace = Path(str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "")).resolve(strict=False)
    cfg = _cfg()
    board = os.environ.get("HERMES_KANBAN_BOARD") or None
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect(board=board)
        try:
            task = kb.get_task(conn, task_id)
            if task is None:
                return _err("current task not found", task_id=task_id)
            expected_workspace = str(getattr(task, "workspace_path", "") or "").strip()
            if expected_workspace and Path(expected_workspace).resolve(strict=False) != workspace:
                return _err("HERMES_KANBAN_WORKSPACE differs from task workspace_path", env_workspace=str(workspace), task_workspace=expected_workspace)
            expected_branch = str(os.environ.get("HERMES_KANBAN_BRANCH") or getattr(task, "branch_name", "") or "").strip() or None
            base_ref = str(args.get("base_ref") or _extract_base_ref(str(getattr(task, "body", "") or "")) or "").strip() or None
            repo_root = None
            if workspace.parent.name == ".worktrees":
                repo_root = workspace.parent.parent.resolve()
            validation = _validate_worktree(
                workspace,
                repo_root=repo_root,
                expected_branch=expected_branch,
                base_ref=base_ref,
                cfg=cfg,
                require_exact_base=False,
            )
            payload = {"event": "verify", "task_id": task_id, "workspace": str(workspace), "passed": bool(validation.get("passed")), "validation": validation}
            _audit(payload)
            if validation.get("passed"):
                return _ok(verified=True, task_id=task_id, validation=validation)
            return _err("worktree integrity verification failed; do not repair or continue", human_required=True, verified=False, task_id=task_id, validation=validation)
        finally:
            conn.close()
    except Exception as exc:
        _audit({"event": "verify_error", "task_id": task_id, "error": f"{type(exc).__name__}: {exc}"[:1500]})
        return _err(f"{type(exc).__name__}: {exc}"[:1500], human_required=True)


def _recover_handler(args: dict, **kwargs: Any) -> str:
    del kwargs
    if not _prepare_available():
        return _err("worktree_guardian_recover_index_lock unavailable outside dispatched implementation-orchestrator")
    root_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    target_id = str(args.get("target_task_id") or "").strip()
    reason = str(args.get("reason") or "").strip()[:500]
    if not re.fullmatch(r"t_[0-9a-fA-F]+", target_id):
        return _err("invalid target_task_id")
    if not reason:
        return _err("reason is required")
    cfg = _cfg()
    board = os.environ.get("HERMES_KANBAN_BOARD") or None
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect(board=board)
        try:
            target = kb.get_task(conn, target_id)
            if target is None:
                return _err("target task not found")
            if target_id == root_id:
                return _err("target must be a related child card")
            if str(getattr(target, "assignee", "") or "") not in ALLOWED_TARGET_PROFILES:
                return _err("target assignee outside guardian allowlist")
            if not _related(conn, root_id, target_id, str(getattr(target, "body", "") or "")):
                return _err("target is not linked/logically owned by active root")
            if str(getattr(target, "status", "") or "") not in {"blocked", "triage"}:
                return _err("index.lock recovery requires blocked/triage target", status=str(getattr(target, "status", "") or ""))
            if getattr(target, "current_run_id", None) is not None or getattr(target, "worker_pid", None) is not None:
                return _err("target still has active run ownership; refusing lock recovery")
            raw_workspace = str(getattr(target, "workspace_path", "") or "").strip()
            if not raw_workspace:
                return _err("target workspace_path missing")
            workspace = Path(raw_workspace).resolve(strict=False)
            repo_root = workspace.parent.parent.resolve() if workspace.parent.name == ".worktrees" else None
            recovery = _recover_stale_index_lock(workspace, cfg, repo_root=repo_root)
            _audit({"event": "orchestrator_index_lock_recovery", "root_task_id": root_id, "target_task_id": target_id, "reason": reason, "recovery": recovery})
            _add_comment(kb, conn, target_id, f"WORKTREE_GUARDIAN_INDEX_LOCK_RECOVERY\nroot_task_id: {root_id}\nreason: {reason}\nresult: {json.dumps(recovery, ensure_ascii=False)[:3000]}\n")
            if not recovery.get("ok"):
                return _err("safe stale index.lock recovery refused", human_required=True, recovery=recovery)
            return _ok(recovered=bool(recovery.get("changed")), recovery=recovery, target_task_id=target_id)
        finally:
            conn.close()
    except Exception as exc:
        _audit({"event": "recover_error", "root_task_id": root_id, "target_task_id": target_id, "error": f"{type(exc).__name__}: {exc}"[:1500]})
        return _err(f"{type(exc).__name__}: {exc}"[:1500])


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="worktree_guardian_prepare",
        toolset=TOOLSET,
        schema=PREPARE_SCHEMA,
        handler=_prepare_handler,
        check_fn=_prepare_available,
        emoji="🛡️",
    )
    ctx.register_tool(
        name="worktree_guardian_verify",
        toolset=TOOLSET,
        schema=VERIFY_SCHEMA,
        handler=_verify_handler,
        check_fn=_verify_available,
        emoji="🛡️",
    )
    ctx.register_tool(
        name="worktree_guardian_recover_index_lock",
        toolset=TOOLSET,
        schema=RECOVER_SCHEMA,
        handler=_recover_handler,
        check_fn=_prepare_available,
        emoji="🛡️",
    )
