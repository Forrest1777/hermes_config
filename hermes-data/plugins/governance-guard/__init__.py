"""Shared deterministic guard for execution-governor deployments.

Designed against Hermes Agent v0.20.2 / v2026.8.16 public plugin hooks.

v0.2.9: fails closed on unreadable/corrupt governance state and preserves uid/gid/mode across atomic JSON replacement.
v0.2.8.2: emits canonical status->ready requeue events so Hermes recent_success respawn guard permits deliberate singleton reuse.
v0.2.8.1: uses one persistent Execution Governor card per Kanban board and a durable per-board case queue.
v0.2.8: attributes post_tool_call telemetry to HERMES_KANBAN_TASK/HERMES_KANBAN_RUN_ID, not the generic session task_id.
v0.2.7: bridges native max-runtime `timed_out` outcomes from dispatch ticks into the same abnormal-run governance pipeline.
v0.1.9: resolves task boards from physical SQLite files, bypassing env-pinned DB overrides.
Real v0.20.2 hooks can report board="default" for named-board workers on both
spawn and exit. Persisted board hints remain authoritative; hook values are
validated against the board that actually contains the task.
Canonical state lives under the sibling execution-governor profile.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import stat
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger("governance-guard")

SCHEMA_VERSION = 1


class GovernanceStateError(RuntimeError):
    """Canonical governance state could not be read safely."""


_DEFAULT_TZ = "America/Sao_Paulo"
_TERMINAL_TYPES = {
    "usage_limit_reached",
    "insufficient_quota",
    "billing_hard_limit_reached",
    "usage_quota_exceeded",
    "quota_exceeded",
}
_NONRETRYABLE_CONFIGURATION_TYPES = {
    "model_not_found",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _governor_home() -> Path:
    """Resolve the execution-governor profile without hard-coding /opt/data."""
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home()).resolve(strict=False)
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", "/opt/data")).resolve(strict=False)

    if home.name == "execution-governor":
        return home
    if home.parent.name == "profiles":
        return home.parent / "execution-governor"
    return home / "profiles" / "execution-governor"


def _governance_dir() -> Path:
    root = _governor_home() / "governance"
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases").mkdir(parents=True, exist_ok=True)
    return root


def _policy_path() -> Path:
    return _governance_dir() / "policy.yaml"


def _state_path() -> Path:
    return _governance_dir() / "state.json"


def _events_path() -> Path:
    return _governance_dir() / "events.jsonl"


@contextmanager
def _file_lock(target: Path):
    """Cross-process advisory lock. Hermes container is Linux; Windows fallback included."""
    lock = target.with_name(f".{target.name}.lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    handle = open(lock, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            if handle.seek(0, os.SEEK_END) == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _load_policy() -> dict[str, Any]:
    try:
        data = yaml.safe_load(_policy_path().read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("Cannot load governance policy: %s", exc)
        return {}


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "providers": {},
        "tasks": {},
        "cases": {},
        "governance_boards": {},
        "last_updated_at": None,
    }


def _normalize_state(data: Any, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise GovernanceStateError(f"STATE_UNAVAILABLE: {path} root is not a JSON object")
    data.setdefault("schema_version", SCHEMA_VERSION)
    data.setdefault("providers", {})
    data.setdefault("tasks", {})
    data.setdefault("cases", {})
    data.setdefault("governance_boards", {})
    return data


def _read_state_unlocked() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # First-run bootstrap is the ONLY read failure that may mean empty state.
        return _default_state()
    except Exception as exc:
        raise GovernanceStateError(
            f"STATE_UNAVAILABLE: cannot read {path}: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        return _normalize_state(json.loads(raw), path)
    except GovernanceStateError:
        raise
    except Exception as exc:
        raise GovernanceStateError(
            f"STATE_UNAVAILABLE: invalid JSON in {path}: {type(exc).__name__}: {exc}"
        ) from exc


def _replacement_metadata(path: Path) -> tuple[int, int, int]:
    """Return uid/gid/mode that an atomic replacement must retain."""
    try:
        st = path.stat()
        return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        parent = path.parent.stat()
        # New governance JSON is private by default while inheriting the
        # governance directory owner/group.
        return parent.st_uid, parent.st_gid, 0o600


def _apply_fd_metadata(fd: int, path: Path) -> None:
    uid, gid, mode = _replacement_metadata(path)
    os.fchmod(fd, mode)
    current = os.fstat(fd)
    if current.st_uid != uid or current.st_gid != gid:
        # Root-run maintenance helpers can safely replace a hermes-owned file
        # without changing ownership. A non-root writer that cannot preserve
        # the target owner/group fails rather than silently changing it.
        os.fchown(fd, uid, gid)


def _fsync_parent(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    dfd = os.open(str(path.parent), flags)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _atomic_json_write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        _apply_fd_metadata(fd, path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
        _fsync_parent(path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _update_state(mutator):
    path = _state_path()
    with _file_lock(path):
        state = _read_state_unlocked()
        result = mutator(state)
        state["last_updated_at"] = _utc_now()
        _atomic_json_write(path, state)
        return result


def _append_event(event: str, **payload: Any) -> None:
    path = _events_path()
    record = {"ts": _utc_now(), "event": event, **payload}
    try:
        with _file_lock(path):
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                handle.flush()
    except Exception as exc:
        logger.warning("Could not append governance event %s: %s", event, exc)


def _profile_governed(profile: str | None) -> bool:
    policy = _load_policy()
    configured = policy.get("governed_profiles") or []
    return bool(profile and profile in {str(x) for x in configured})


def _run_id() -> str | None:
    value = os.environ.get("HERMES_KANBAN_RUN_ID")
    return str(value) if value else None


def _task_id() -> str | None:
    value = os.environ.get("HERMES_KANBAN_TASK")
    return str(value) if value else None


def _flatten_values(value: Any):
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k).lower(), v
            yield from _flatten_values(v)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _flatten_values(item)


def _coerce_body(body: Any) -> Any:
    if isinstance(body, str):
        try:
            return json.loads(body)
        except Exception:
            return body
    return body


def _extract_provider_error(error_type=None, error_code=None, error_message=None, error_body=None) -> dict[str, Any]:
    body = _coerce_body(error_body)
    types = []
    if error_type:
        types.append(str(error_type).strip().lower())
    if error_code:
        types.append(str(error_code).strip().lower())

    plan_type = None
    resets_at = None
    for key, value in _flatten_values(body):
        if key in {"type", "code", "error_type", "error_code"} and value is not None:
            types.append(str(value).strip().lower())
        elif key == "plan_type" and value is not None and plan_type is None:
            plan_type = str(value)
        elif key == "resets_at" and resets_at is None:
            try:
                resets_at = int(value)
            except (TypeError, ValueError):
                pass

    message = str(error_message or "")
    lower = message.lower()
    terminal = next((t for t in types if t in _TERMINAL_TYPES), None)
    if terminal is None:
        phrase_map = {
            "usage_limit_reached": ("usage limit has been reached", "usage_limit_reached"),
            "insufficient_quota": ("insufficient_quota",),
            "billing_hard_limit_reached": ("billing hard limit",),
        }
        for kind, phrases in phrase_map.items():
            if any(p in lower for p in phrases):
                terminal = kind
                break

    return {
        "terminal": terminal is not None,
        "error_type": terminal or (types[0] if types else None),
        "types": list(dict.fromkeys(types)),
        "plan_type": plan_type,
        "resets_at": resets_at,
        "message": message,
    }


def _format_reset(ts: int | None) -> tuple[str | None, str]:
    policy = _load_policy()
    tz_name = str(policy.get("timezone") or _DEFAULT_TZ)
    if ts is None:
        return None, tz_name
    try:
        from zoneinfo import ZoneInfo
        local = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(ZoneInfo(tz_name))
        offset = local.utcoffset()
        total_minutes = int(offset.total_seconds() // 60) if offset is not None else 0
        sign = "+" if total_minutes >= 0 else "−"
        total_minutes = abs(total_minutes)
        offset_text = f"UTC{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"
        label = "horário de Brasília" if tz_name == "America/Sao_Paulo" else tz_name
        return f"{local.strftime('%d/%m/%Y às %H:%M:%S')} no {label} ({offset_text})", tz_name
    except Exception:
        local = datetime.fromtimestamp(int(ts), timezone.utc)
        return local.strftime("%d/%m/%Y às %H:%M:%S UTC"), "UTC"


# HERMES_OPERATIONAL_HARDENING_2026_09_03: provider budget is a deferred retry state, not an immediate human gate.
def _provider_recovery_config(policy: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = ((policy or _load_policy()).get("provider_recovery") or {})
    try:
        interval_minutes = max(1, int(raw.get("retry_interval_minutes") or 20))
    except Exception:
        interval_minutes = 20
    try:
        max_retries = max(0, int(raw.get("max_automatic_retries") or 12))
    except Exception:
        max_retries = 12
    try:
        ttl_minutes = max(5, int(raw.get("authorization_ttl_minutes") or 60))
    except Exception:
        ttl_minutes = 60
    return {
        "enabled": bool(raw.get("enabled", True)),
        "retry_interval_seconds": interval_minutes * 60,
        "max_automatic_retries": max_retries,
        "prefer_provider_reset_time": bool(raw.get("prefer_provider_reset_time", False)),
        "authorization_ttl_seconds": ttl_minutes * 60,
        "one_probe_per_provider_per_dispatch_tick": bool(
            raw.get("one_probe_per_provider_per_dispatch_tick", True)
        ),
    }


def _next_provider_retry_at(info: dict[str, Any], recovery: dict[str, Any]) -> int:
    now_epoch = int(time.time())
    reset = info.get("resets_at")
    if recovery.get("prefer_provider_reset_time") and reset is not None:
        try:
            reset_epoch = int(reset)
            if reset_epoch > now_epoch:
                return reset_epoch
        except Exception:
            pass
    return now_epoch + int(recovery.get("retry_interval_seconds") or 1200)


def _open_provider_circuit(provider: str, model: str | None, info: dict[str, Any]) -> None:
    task_id = _task_id()
    local_reset, tz_name = _format_reset(info.get("resets_at"))
    recovery = _provider_recovery_config()
    next_retry_at = _next_provider_retry_at(info, recovery) if recovery.get("enabled") else None

    def mutate(state):
        prior = (state.setdefault("providers", {}).get(provider) or {})
        state["providers"][provider] = {
            "state": "OPEN",
            "reason": info.get("error_type") or "provider_budget_exhausted",
            "model": model,
            "opened_at": _utc_now(),
            "opened_count": int(prior.get("opened_count") or 0) + 1,
            "task_id": task_id,
            "plan_type": info.get("plan_type"),
            "resets_at": info.get("resets_at"),
            "resets_at_local": local_reset,
            "timezone": tz_name,
            "next_retry_at": next_retry_at,
            "human_reset_required": not bool(recovery.get("enabled")),
        }

    _update_state(mutate)
    _append_event(
        "provider_circuit_opened",
        provider=provider,
        model=model,
        task_id=task_id,
        reason=info.get("error_type"),
        plan_type=info.get("plan_type"),
        resets_at=info.get("resets_at"),
        resets_at_local=local_reset,
        timezone=tz_name,
        next_retry_at=next_retry_at,
        automatic_recovery=bool(recovery.get("enabled")),
    )


def _classify_api_error(provider=None, model=None, error_type=None, error_code=None,
                        error_message=None, error_body=None, **kwargs):
    del kwargs
    info = _extract_provider_error(error_type, error_code, error_message, error_body)
    provider_name = str(provider or "unknown")

    configuration_error = next(
        (kind for kind in info.get("types", []) if kind in _NONRETRYABLE_CONFIGURATION_TYPES),
        None,
    )
    if not info["terminal"] and configuration_error:
        _append_event(
            "provider_nonretryable_error",
            provider=provider_name,
            model=model,
            task_id=_task_id(),
            reason=configuration_error,
        )
        return {
            "reason": "invalid_request",
            "retryable": False,
            "should_compress": False,
            "should_rotate_credential": False,
            "should_fallback": False,
            "message": (
                f"NON-RETRYABLE PROVIDER CONFIGURATION ERROR\n"
                f"Provider: {provider_name}\n"
                f"Type: {configuration_error}\n"
                "Automatic retries: DISABLED\n"
                "Correct the model/provider configuration before retrying."
            ),
            "error_context": {
                "governance": "provider_configuration_error",
                "provider": provider_name,
                "model": model,
                "error_type": configuration_error,
                "human_intervention_required": True,
            },
        }

    if not info["terminal"]:
        return None

    state_unavailable = None
    try:
        _open_provider_circuit(provider_name, str(model) if model else None, info)
    except GovernanceStateError as exc:
        # Classification itself must remain fail-closed even if the canonical
        # provider circuit cannot be persisted. Never fall back to normal SDK
        # retry behavior merely because state.json is unavailable.
        state_unavailable = str(exc)[:1000]
        _append_event(
            "governance_state_unavailable",
            task_id=_task_id(),
            provider=provider_name,
            model=model,
            fail_closed=True,
            error=state_unavailable,
            source="transform_api_error_classification",
        )

    reset_text, tz_name = _format_reset(info.get("resets_at"))
    parts = [
        "PROVIDER BUDGET EXHAUSTED",
        f"Provider: {provider_name}",
        f"Type: {info.get('error_type')}",
    ]
    if info.get("plan_type"):
        parts.append(f"Plan: {info['plan_type']}")
    if reset_text:
        parts.append(f"Reset informado pelo provider: {reset_text} [{tz_name}]")
    recovery = _provider_recovery_config()
    next_retry_at = _next_provider_retry_at(info, recovery) if recovery.get("enabled") else None
    if recovery.get("enabled"):
        parts.extend([
            "Immediate retries: DISABLED",
            "Provider circuit: OPEN / WAITING_PROVIDER",
            f"Automatic recovery scheduled for epoch {next_retry_at}.",
        ])
    else:
        parts.extend([
            "Automatic retries: DISABLED",
            "Provider circuit: OPEN",
            "Human intervention required before this provider is reused.",
        ])
    return {
        "reason": "billing",
        "retryable": False,
        "should_compress": False,
        "should_rotate_credential": False,
        "should_fallback": False,
        "message": "\n".join(parts),
        "error_context": {
            "governance": "provider_budget_exhausted",
            "provider": provider_name,
            "model": model,
            "plan_type": info.get("plan_type"),
            "resets_at": info.get("resets_at"),
            "resets_at_local": reset_text,
            "timezone": tz_name,
            "next_retry_at": next_retry_at,
            "automatic_recovery_enabled": bool(recovery.get("enabled")),
            "human_intervention_required": not bool(recovery.get("enabled")),
            "state_unavailable": bool(state_unavailable),
        },
    }


def _api_request_error(provider=None, model=None, status_code=None, retry_count=None,
                       max_retries=None, reason=None, error=None, **kwargs):
    del kwargs
    tid = _task_id()
    if not tid:
        return
    rid = str(_run_id() or "")
    profile = os.environ.get("HERMES_PROFILE")
    payload = {
        "provider": provider,
        "model": model,
        "status_code": status_code,
        "retry_count": retry_count,
        "max_retries": max_retries,
        "reason": str(reason) if reason is not None else None,
        "error": str(error)[:1200] if error is not None else None,
        "observed_at": _utc_now(),
    }
    state_error = None
    if rid:
        def mutate(state):
            task = state.setdefault("tasks", {}).setdefault(str(tid), {"total_attempts": 0, "runs": {}})
            run = task.setdefault("runs", {}).setdefault(rid, {"run_id": _run_id(), "profile": profile})
            run["last_api_error"] = payload
        try:
            _update_state(mutate)
        except GovernanceStateError as exc:
            state_error = str(exc)[:1000]
            _append_event(
                "governance_state_unavailable",
                task_id=tid,
                run_id=_run_id(),
                profile=profile,
                fail_closed=True,
                error=state_error,
                source="api_request_error",
            )
    _append_event(
        "api_request_error",
        task_id=tid,
        run_id=_run_id(),
        profile=profile,
        state_unavailable=bool(state_error),
        **payload,
    )

# HERMES_GIT_SNAPSHOT_TIMEOUT_2026_09_07
def _git_snapshot_timeout_seconds() -> int:
    try:
        raw = ((_load_policy().get("progress") or {}).get("git_snapshot_timeout_seconds") or 30)
        return max(5, int(raw))
    except Exception:
        return 30


def _git_snapshot(workspace_path: str | None) -> dict[str, Any]:
    if not workspace_path:
        return {"available": False, "reason": "no_workspace"}
    path = Path(workspace_path)
    if not path.exists():
        return {"available": False, "reason": "workspace_missing", "path": workspace_path}

    def run(*args: str) -> str:
        completed = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_git_snapshot_timeout_seconds(),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "git failed").strip()[:500])
        return completed.stdout

    try:
        head = run("rev-parse", "HEAD").strip()
        status = run("status", "--porcelain=v1", "--untracked-files=all")
        return {
            "available": True,
            "path": workspace_path,
            "head": head,
            "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
            "changed_entries": len([line for line in status.splitlines() if line.strip()]),
        }
    except Exception as exc:
        return {"available": False, "path": workspace_path, "reason": str(exc)[:500]}


def _on_worker_spawned(task_id=None, assignee=None, worker_pid=None, workspace_path=None,
                       run_id=None, board=None, **kwargs):
    del kwargs
    profile = str(assignee or "")
    if not _profile_governed(profile):
        return
    tid = str(task_id)
    rid = str(run_id) if run_id is not None else f"pid:{worker_pid}"
    before = _git_snapshot(workspace_path)
    holder = {}

    def mutate(state):
        task = state.setdefault("tasks", {}).setdefault(tid, {"total_attempts": 0, "runs": {}})
        hinted_board = task.get("board_hint")
        hinted_tenant = task.get("tenant_hint")
        effective_board = _normalized_board(hinted_board or board)
        scope = _task_scope(tid, effective_board) if not hinted_tenant else {
            "board": effective_board, "tenant": hinted_tenant, "title": None, "scope_status": "persisted_tenant_hint"
        }
        tenant = hinted_tenant if hinted_tenant is not None else scope.get("tenant")
        runs = task.setdefault("runs", {})
        if rid not in runs:
            task["total_attempts"] = int(task.get("total_attempts") or 0) + 1
        runs[rid] = {
            **(runs.get(rid) or {}), "run_id": run_id, "profile": profile,
            "board": effective_board, "tenant": tenant, "worker_pid": worker_pid,
            "workspace_path": workspace_path, "started_at": _utc_now(), "before": before,
            "tool_calls": int((runs.get(rid) or {}).get("tool_calls") or 0),
            "tool_counts": dict((runs.get(rid) or {}).get("tool_counts") or {}),
        }
        authorization = task.get("authorized_recovery_checkpoint")
        if isinstance(authorization, dict) and authorization.get("state") == "AUTHORIZED":
            authorization["state"] = "IN_USE"
            authorization["run_id"] = rid
            authorization["claimed_at"] = _utc_now()
            runs[rid]["resume_epoch"] = authorization.get("resume_epoch")
            wait = task.get("provider_wait")
            if isinstance(wait, dict) and wait.get("state") == "RESUMING":
                wait["state"] = "RUNNING"
                wait["run_id"] = rid
        holder.update(board=effective_board, tenant=tenant, scope_status=scope.get("scope_status"))

    try:
        _update_state(mutate)
    except GovernanceStateError as exc:
        _append_event(
            "governance_state_unavailable",
            task_id=tid,
            run_id=run_id,
            profile=profile,
            board=_normalized_board(board),
            fail_closed=True,
            error=str(exc)[:1200],
            source="worker_spawned",
        )
        return
    if _normalized_board(board) != holder.get("board"):
        _append_event("worker_spawn_board_mismatch", task_id=tid, run_id=run_id, profile=profile,
                      hook_board=_normalized_board(board), effective_board=holder.get("board"),
                      resolution="persisted_board_hint")
    _append_event("attempt_started", task_id=tid, run_id=run_id, profile=profile,
                  board=holder.get("board"), tenant=holder.get("tenant"),
                  scope_status=holder.get("scope_status"), worker_pid=worker_pid, before=before)

def _post_tool_call(tool_name=None, task_id=None, **kwargs):
    del kwargs

    # In a dispatcher-owned Kanban worker, post_tool_call's `task_id` is a
    # generic agent/session identifier and is not guaranteed to be the durable
    # Kanban card id. The dispatcher explicitly pins HERMES_KANBAN_TASK and
    # HERMES_KANBAN_RUN_ID, so those are authoritative for the governance
    # attempt ledger.
    kanban_tid = str(_task_id() or "")
    hook_tid = str(task_id or "")
    tid = kanban_tid or hook_tid
    rid = str(_run_id() or "")
    if not tid or not rid:
        return

    profile = os.environ.get("HERMES_PROFILE")
    if not _profile_governed(profile):
        return

    def mutate(state):
        task = state.setdefault("tasks", {}).setdefault(tid, {"total_attempts": 0, "runs": {}})
        run = task.setdefault("runs", {}).setdefault(rid, {
            "run_id": int(rid) if rid.isdigit() else rid,
            "profile": profile,
            "started_at": _utc_now(),
            "tool_calls": 0,
            "tool_counts": {},
        })
        run["tool_calls"] = int(run.get("tool_calls") or 0) + 1
        counts = run.setdefault("tool_counts", {})
        name = str(tool_name or "unknown")
        counts[name] = int(counts.get(name) or 0) + 1

        # Keep the hook/session id only as diagnostic metadata, never as the
        # canonical task key.
        if kanban_tid and hook_tid and hook_tid != kanban_tid:
            aliases = run.setdefault("hook_task_id_aliases", {})
            aliases[hook_tid] = int(aliases.get(hook_tid) or 0) + 1

    _update_state(mutate)


def _latest_open_provider_for_task(state: dict[str, Any], task_id: str) -> dict[str, Any] | None:
    opened = []
    for provider, entry in (state.get("providers") or {}).items():
        if isinstance(entry, dict) and entry.get("state") == "OPEN" and entry.get("task_id") == task_id:
            opened.append((provider, entry))
    if not opened:
        return None
    provider, entry = opened[-1]
    return {"provider": provider, **entry}


def _compute_progress(before: dict[str, Any] | None, after: dict[str, Any] | None,
                      tool_calls: int, tool_counts: dict[str, int] | None = None,
                      previous_after: dict[str, Any] | None = None) -> dict[str, Any]:
    before = before or {}
    after = after or {}
    tool_counts = tool_counts or {}
    progress_tools = {
        "kanban_create", "kanban_link", "kanban_complete", "kanban_block",
        "kanban_unblock", "kanban_request_review", "kanban_request_changes",
    }
    kanban_progress_events = sum(int(tool_counts.get(name) or 0) for name in progress_tools)
    if before.get("available") and after.get("available"):
        head_changed = before.get("head") != after.get("head")
        status_changed = before.get("status_sha256") != after.get("status_sha256")
        material = bool(head_changed or status_changed or kanban_progress_events)
        result = {
            "material_progress": material,
            "confidence": "high",
            "head_changed": head_changed,
            "git_status_changed": status_changed,
            "kanban_progress_events": kanban_progress_events,
            "tool_calls": tool_calls,
        }
        if previous_after and previous_after.get("available") and after.get("available"):
            result["same_as_previous_attempt_end"] = bool(
                previous_after.get("head") == after.get("head")
                and previous_after.get("status_sha256") == after.get("status_sha256")
            )
        return result
    if kanban_progress_events:
        return {
            "material_progress": True,
            "confidence": "medium",
            "reason": "kanban_state_mutation_observed",
            "kanban_progress_events": kanban_progress_events,
            "tool_calls": tool_calls,
        }
    if tool_calls == 0:
        return {
            "material_progress": False,
            "confidence": "medium",
            "reason": "no_tool_calls_and_git_snapshot_unavailable",
            "tool_calls": 0,
        }
    return {
        "material_progress": None,
        "confidence": "low",
        "reason": "git_snapshot_unavailable",
        "tool_calls": tool_calls,
    }


def _write_case(case: dict[str, Any]) -> Path:
    path = _governance_dir() / "cases" / f"{case['case_id']}.json"
    with _file_lock(path):
        _atomic_json_write(path, case)
    return path


def _hold_task(ctx, task_id: str, board: str | None, reason: str, kind: str) -> dict[str, Any]:
    del ctx
    effective_board = _normalized_board(board)
    conflict = _named_board_global_override_conflict(effective_board)
    if conflict:
        return {"ok": False, "skipped": "global_kanban_override_conflict", "board": effective_board, **conflict}
    try:
        from hermes_cli import kanban_db as kb
        # HERMES_V021_KANBAN_CONNECT_2026_09_07
        from hermes_cli.kanban_db_connect import connect as _kanban_connect
        conn = _kanban_connect(board=effective_board)
        try:
            task = kb.get_task(conn, task_id)
            if task is None:
                return {"ok": False, "error": "task_not_found", "board": effective_board}
            if str(task.status) == "blocked":
                return {"ok": True, "method": "already_blocked", "board": effective_board}
            ok = kb.block_task(conn, task_id, reason=reason, kind=kind)
            return {"ok": bool(ok), "method": "kanban_db.block_task", "board": effective_board}
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "board": effective_board, "error": str(exc)[:1000]}

def _normalized_board(board: str | None) -> str:
    return str(board or "default").strip() or "default"


def _named_board_global_override_conflict(board: str | None) -> dict[str, Any] | None:
    """Detect container-wide pins that defeat Hermes named-board routing.

    Dispatcher-spawned workers receive scoped HERMES_KANBAN_* variables by
    design, so they are allowed when HERMES_KANBAN_TASK is present.
    """
    b = _normalized_board(board)
    if b == "default" or os.environ.get("HERMES_KANBAN_TASK"):
        return None
    pinned_db = str(os.environ.get("HERMES_KANBAN_DB") or "").strip()
    pinned_ws = str(os.environ.get("HERMES_KANBAN_WORKSPACES_ROOT") or "").strip()
    if not pinned_db and not pinned_ws:
        return None
    return {"pinned_db": bool(pinned_db), "pinned_workspaces_root": bool(pinned_ws)}


def _task_scope(task_id: str, board: str | None) -> dict[str, Any]:
    b = _normalized_board(board)
    conflict = _named_board_global_override_conflict(b)
    if conflict:
        return {"board": b, "tenant": None, "title": None,
                "scope_status": "global_kanban_override_conflict", **conflict}
    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=b)
        try:
            task = kb.get_task(conn, task_id)
            if task is None:
                return {"board": b, "tenant": None, "title": None, "scope_status": "task_not_found"}
            return {
                "board": b,
                "tenant": getattr(task, "tenant", None),
                "title": getattr(task, "title", None),
                "scope_status": "task_verified",
            }
        finally:
            conn.close()
    except Exception as exc:
        return {"board": b, "tenant": None, "title": None,
                "scope_status": f"scope_lookup_error:{type(exc).__name__}"}


def _value_in_list(value: str | None, values: Any, default_if_empty: bool = False) -> bool:
    if not isinstance(values, (list, tuple, set)):
        return default_if_empty
    wanted = {str(v).strip() for v in values if str(v).strip()}
    if not wanted:
        return default_if_empty
    return "*" in wanted or str(value or "").strip() in wanted


def _enforcement_allowed(actions: dict[str, Any], board: str | None, tenant: str | None = None) -> bool:
    if str(actions.get("mode") or "observe").strip().lower() != "enforce":
        return False
    if not _value_in_list(_normalized_board(board), actions.get("enforce_boards") or [], False):
        return False
    return _value_in_list(tenant, actions.get("enforce_tenants") or [], True)


def _auto_create_allowed(actions: dict[str, Any], board: str | None, tenant: str | None = None) -> bool:
    cfg = actions.get("governance_cards") or {}
    if not bool(cfg.get("auto_create", False)):
        return False
    if not _value_in_list(_normalized_board(board), cfg.get("auto_create_boards") or [], False):
        return False
    return _value_in_list(tenant, cfg.get("auto_create_tenants") or [], True)



# HERMES_DIRTY_CHECKPOINT_RECOVERY_2026_09_04
DIRTY_RECOVERY_TOOLSET = "orchestration_control"
DIRTY_RECOVERY_ALLOWED_TARGET_PROFILES = {"implementation-worker", "implementation-architect"}

DIRTY_RECOVERY_SCHEMA = {
    "name": "request_dirty_checkpoint_recovery",
    "description": (
        "Request Execution Governor review for a preserved dirty checkpoint on a related child card. "
        "This request never authorizes retry and never reactivates the target by itself."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "target_task_id": {"type": "string", "description": "Related blocked/triage child task id."},
            "reason": {"type": "string", "description": "Short reason for requesting governed recovery."},
        },
        "required": ["target_task_id", "reason"],
        "additionalProperties": False,
    },
}


def _dirty_recovery_tool_available() -> bool:
    return (
        os.environ.get("HERMES_PROFILE") == "implementation-orchestrator"
        and bool(os.environ.get("HERMES_KANBAN_TASK"))
    )


def _dirty_recovery_related(conn, root_id: str, target_id: str, target_body: str) -> bool:
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
    return any(re.search(pattern, target_body or "") for pattern in patterns)


def _dirty_recovery_latest_run(task_state: dict[str, Any]) -> dict[str, Any] | None:
    runs = task_state.get("runs") or {}
    if not isinstance(runs, dict) or not runs:
        return None
    values = [value for value in runs.values() if isinstance(value, dict)]
    if not values:
        return None
    return max(
        values,
        key=lambda value: (
            str(value.get("started_at") or ""),
            int(value.get("run_id")) if str(value.get("run_id") or "").isdigit() else -1,
        ),
    )


def _request_dirty_checkpoint_recovery(args: dict, **kwargs) -> str:
    del kwargs
    if not _dirty_recovery_tool_available():
        return json.dumps({"ok": False, "error": "dirty recovery request unavailable outside implementation-orchestrator"}, ensure_ascii=False)

    root_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    target_id = str(args.get("target_task_id") or "").strip()
    reason = str(args.get("reason") or "").strip()[:500]
    if not re.fullmatch(r"t_[0-9a-fA-F]+", target_id):
        return json.dumps({"ok": False, "error": "invalid target_task_id"}, ensure_ascii=False)
    if not reason:
        return json.dumps({"ok": False, "error": "reason is required"}, ensure_ascii=False)
    if target_id == root_id:
        return json.dumps({"ok": False, "error": "target must be a related child card"}, ensure_ascii=False)

    board = _normalized_board(os.environ.get("HERMES_KANBAN_BOARD"))
    policy = _load_policy()
    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=board)
        try:
            root = kb.get_task(conn, root_id)
            target = kb.get_task(conn, target_id)
            if root is None or target is None:
                return json.dumps({"ok": False, "error": "root or target task not found"}, ensure_ascii=False)
            if str(getattr(root, "assignee", "") or "") != "implementation-orchestrator":
                return json.dumps({"ok": False, "error": "active root is not implementation-orchestrator"}, ensure_ascii=False)
            target_profile = str(getattr(target, "assignee", "") or "")
            if target_profile not in DIRTY_RECOVERY_ALLOWED_TARGET_PROFILES:
                return json.dumps({"ok": False, "error": "target profile is outside dirty recovery allowlist", "profile": target_profile}, ensure_ascii=False)
            target_status = str(getattr(target, "status", "") or "")
            if target_status not in {"blocked", "triage"}:
                return json.dumps({"ok": False, "error": "target must be blocked or triage", "status": target_status}, ensure_ascii=False)
            if getattr(target, "current_run_id", None) is not None:
                return json.dumps({"ok": False, "error": "target still has current_run_id"}, ensure_ascii=False)
            if not _dirty_recovery_related(conn, root_id, target_id, str(getattr(target, "body", "") or "")):
                return json.dumps({"ok": False, "error": "target is not related to active orchestrator root"}, ensure_ascii=False)
            tenant = getattr(target, "tenant", None)
        finally:
            conn.close()

        with _file_lock(_state_path()):
            state_snapshot = _read_state_unlocked()
        target_state = (state_snapshot.get("tasks") or {}).get(target_id)
        if not isinstance(target_state, dict):
            return json.dumps({"ok": False, "error": "governance task ledger missing target"}, ensure_ascii=False)
        latest_run = _dirty_recovery_latest_run(target_state)
        if latest_run is None:
            return json.dumps({"ok": False, "error": "target has no governed prior run"}, ensure_ascii=False)
        workspace = str(latest_run.get("workspace_path") or "").strip()
        checkpoint = _git_snapshot(workspace)
        if not checkpoint.get("available"):
            return json.dumps({"ok": False, "error": "checkpoint snapshot unavailable", "checkpoint": checkpoint}, ensure_ascii=False)
        if int(checkpoint.get("changed_entries") or 0) <= 0:
            return json.dumps({"ok": False, "error": "target worktree is not dirty"}, ensure_ascii=False)
        if not checkpoint.get("head") or not checkpoint.get("status_sha256"):
            return json.dumps({"ok": False, "error": "checkpoint fingerprint incomplete"}, ensure_ascii=False)

        status_sha256 = str(checkpoint["status_sha256"])
        case_id = f"dirty-recovery-{target_id}-{status_sha256[:16]}"
        existing_meta = (state_snapshot.get("cases") or {}).get(case_id)
        if isinstance(existing_meta, dict):
            existing_status = str(existing_meta.get("status") or "")
            if existing_status != "PENDING":
                return json.dumps({
                    "ok": False,
                    "error": "same checkpoint already has a terminal governance decision",
                    "case_id": case_id,
                    "case_status": existing_status,
                }, ensure_ascii=False)
            existing_case = json.loads((_governance_dir() / "cases" / f"{case_id}.json").read_text(encoding="utf-8"))
            ensured = _ensure_board_governor(None, existing_case, board, policy)
            _append_event("dirty_checkpoint_recovery_request_reused", root_task_id=root_id, task_id=target_id, case_id=case_id, result=ensured)
            return json.dumps({"ok": bool(ensured.get("ok")), "case_id": case_id, "reused": True, "checkpoint": checkpoint, "governor": ensured}, ensure_ascii=False)

        attempt = dict(latest_run)
        attempt["after"] = dict(checkpoint)
        progress = {
            "material_progress": True,
            "confidence": "high",
            "reason": "preserved_dirty_checkpoint",
            "git_status_changed": True,
            "tool_calls": int(latest_run.get("tool_calls") or 0),
        }
        provider_circuit = _latest_open_provider_for_task(state_snapshot, target_id)
        case = {
            "case_id": case_id,
            "case_type": "DIRTY_CHECKPOINT_RECOVERY",
            "status": "PENDING",
            "created_at": _utc_now(),
            "task_id": target_id,
            "run_id": latest_run.get("run_id"),
            "profile": target_profile,
            "board": board,
            "tenant": tenant,
            "worker_pid": latest_run.get("worker_pid"),
            "exit": {
                "kind": "dirty_checkpoint_recovery_request",
                "code": None,
                "outcome": target_status,
                "retry_status": "recovery_requested",
            },
            "total_attempts": int(target_state.get("total_attempts") or 0),
            "max_total_attempts": int(((policy.get("task_budget") or {}).get("default_max_total_attempts")) or 2),
            "progress": progress,
            "provider_circuit": provider_circuit,
            "last_api_error": latest_run.get("last_api_error"),
            "attempt": attempt,
            "recovery_request": {
                "validated": True,
                "requested_by_task_id": root_id,
                "requested_by_profile": "implementation-orchestrator",
                "requested_at": _utc_now(),
                "reason": reason,
                "target_status": target_status,
                "related_to_root": True,
                "checkpoint": dict(checkpoint),
            },
            "decision": None,
        }
        case_path = _write_case(case)

        def remember(state):
            state.setdefault("cases", {})[case_id] = {
                "task_id": target_id,
                "run_id": latest_run.get("run_id"),
                "status": "PENDING",
                "path": str(case_path),
            }
            task_entry = state.setdefault("tasks", {}).setdefault(target_id, {"total_attempts": 0, "runs": {}})
            task_entry["last_dirty_recovery_request_case_id"] = case_id
            task_entry["last_dirty_recovery_request_at"] = _utc_now()
        _update_state(remember)

        ensured = _ensure_board_governor(None, case, board, policy)
        _append_event(
            "dirty_checkpoint_recovery_requested",
            root_task_id=root_id,
            task_id=target_id,
            case_id=case_id,
            target_status=target_status,
            workspace=checkpoint.get("path"),
            head=checkpoint.get("head"),
            status_sha256=checkpoint.get("status_sha256"),
            result=ensured,
        )
        if not ensured.get("ok"):
            return json.dumps({"ok": False, "error": "governance case created but Governor could not be ensured", "case_id": case_id, "governor": ensured}, ensure_ascii=False)
        return json.dumps({
            "ok": True,
            "action": "REQUEST_DIRTY_CHECKPOINT_RECOVERY",
            "case_id": case_id,
            "target_task_id": target_id,
            "target_status": target_status,
            "checkpoint": checkpoint,
            "governor": ensured,
            "retry_authorized": False,
            "target_reactivated": False,
        }, ensure_ascii=False)
    except GovernanceStateError as exc:
        _append_event("dirty_checkpoint_recovery_request_failed", root_task_id=root_id, task_id=target_id, fail_closed=True, error=str(exc)[:1000])
        return json.dumps({"ok": False, "error": "STATE_UNAVAILABLE", "detail": str(exc)[:1000]}, ensure_ascii=False)
    except Exception as exc:
        _append_event("dirty_checkpoint_recovery_request_failed", root_task_id=root_id, task_id=target_id, fail_closed=True, error=f"{type(exc).__name__}: {exc}"[:1000])
        return json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}, ensure_ascii=False)


# HERMES_DIRTY_CHECKPOINT_RECOVERY_V2_2026_09_04
_DIRTY_RECOVERY_LAST_CATCHUP_MONOTONIC = 0.0


def _dirty_recovery_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    value = policy.get("dirty_checkpoint_recovery") or {}
    return value if isinstance(value, dict) else {}


def _ensure_dirty_recovery_case_for_task(
    task_id: str,
    board: str | None,
    *,
    reason: str,
    source: str,
) -> dict[str, Any]:
    """Create/reuse a recovery case from a trusted durable guard fact.

    This function NEVER authorizes a retry and NEVER reactivates the task.
    Execution Governor remains the only authority that can produce
    authorized_recovery_checkpoint + resume_epoch.
    """
    tid = str(task_id or "").strip()
    if not re.fullmatch(r"t_[0-9a-fA-F]+", tid):
        return {"ok": False, "error": "invalid_task_id"}

    effective_board = _normalized_board(board)
    policy = _load_policy()
    cfg = _dirty_recovery_cfg(policy)
    if not bool(cfg.get("event_driven", True)):
        return {"ok": False, "skipped": "event_driven_disabled"}

    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=effective_board)
        try:
            task = kb.get_task(conn, tid)
            if task is None:
                return {"ok": False, "error": "task_not_found"}
            profile = str(getattr(task, "assignee", "") or "")
            if profile not in DIRTY_RECOVERY_ALLOWED_TARGET_PROFILES:
                return {"ok": False, "skipped": "profile_not_allowed", "profile": profile}
            status = str(getattr(task, "status", "") or "")
            if status not in {"blocked", "triage"}:
                return {"ok": False, "skipped": "task_not_blocked_or_triage", "status": status}
            if getattr(task, "current_run_id", None) is not None:
                return {"ok": False, "skipped": "task_still_running"}
            tenant = getattr(task, "tenant", None)
        finally:
            conn.close()

        with _file_lock(_state_path()):
            state_snapshot = _read_state_unlocked()
        target_state = (state_snapshot.get("tasks") or {}).get(tid)
        if not isinstance(target_state, dict):
            return {"ok": False, "error": "governance_task_ledger_missing"}
        latest_run = _dirty_recovery_latest_run(target_state)
        if latest_run is None:
            return {"ok": False, "error": "governed_prior_run_missing"}

        workspace = str(latest_run.get("workspace_path") or "").strip()
        checkpoint = _git_snapshot(workspace)
        if not checkpoint.get("available"):
            return {"ok": False, "error": "checkpoint_snapshot_unavailable", "checkpoint": checkpoint}
        if int(checkpoint.get("changed_entries") or 0) <= 0:
            return {"ok": False, "skipped": "checkpoint_no_longer_dirty"}
        if not checkpoint.get("head") or not checkpoint.get("status_sha256"):
            return {"ok": False, "error": "checkpoint_fingerprint_incomplete"}

        status_sha256 = str(checkpoint["status_sha256"])
        case_id = f"dirty-recovery-{tid}-{status_sha256[:16]}"
        existing_meta = (state_snapshot.get("cases") or {}).get(case_id)
        if isinstance(existing_meta, dict):
            existing_status = str(existing_meta.get("status") or "")
            if existing_status == "PENDING":
                case_path = _governance_dir() / "cases" / f"{case_id}.json"
                try:
                    existing_case = json.loads(case_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    return {"ok": False, "error": f"existing_case_unreadable:{type(exc).__name__}:{exc}"[:900]}
                ensured = _ensure_board_governor(None, existing_case, effective_board, policy)
                return {
                    "ok": bool(ensured.get("ok")),
                    "case_id": case_id,
                    "reused": True,
                    "case_status": existing_status,
                    "checkpoint": checkpoint,
                    "governor": ensured,
                }
            return {
                "ok": True,
                "case_id": case_id,
                "reused": True,
                "terminal": True,
                "case_status": existing_status,
                "checkpoint": checkpoint,
            }

        attempt = dict(latest_run)
        attempt["after"] = dict(checkpoint)
        progress = {
            "material_progress": True,
            "confidence": "high",
            "reason": "preserved_dirty_checkpoint",
            "git_status_changed": True,
            "tool_calls": int(latest_run.get("tool_calls") or 0),
        }
        case = {
            "case_id": case_id,
            "case_type": "DIRTY_CHECKPOINT_RECOVERY",
            "status": "PENDING",
            "created_at": _utc_now(),
            "task_id": tid,
            "run_id": latest_run.get("run_id"),
            "profile": profile,
            "board": effective_board,
            "tenant": tenant,
            "worker_pid": latest_run.get("worker_pid"),
            "exit": {
                "kind": "retry_checkpoint_guard_block",
                "code": None,
                "outcome": status,
                "retry_status": "recovery_requested",
            },
            "total_attempts": int(target_state.get("total_attempts") or 0),
            "max_total_attempts": int(((policy.get("task_budget") or {}).get("default_max_total_attempts")) or 2),
            "progress": progress,
            "provider_circuit": _latest_open_provider_for_task(state_snapshot, tid),
            "last_api_error": latest_run.get("last_api_error"),
            "attempt": attempt,
            "recovery_request": {
                "validated": True,
                "requested_by_task_id": None,
                "requested_by_profile": "retry-checkpoint-guard",
                "requested_at": _utc_now(),
                "reason": str(reason or "")[:500],
                "source": source,
                "target_status": status,
                "related_to_root": None,
                "checkpoint": dict(checkpoint),
            },
            "decision": None,
        }
        case_path = _write_case(case)

        def remember(state):
            state.setdefault("cases", {})[case_id] = {
                "task_id": tid,
                "run_id": latest_run.get("run_id"),
                "status": "PENDING",
                "path": str(case_path),
            }
            task_entry = state.setdefault("tasks", {}).setdefault(tid, {"total_attempts": 0, "runs": {}})
            task_entry["last_dirty_recovery_request_case_id"] = case_id
            task_entry["last_dirty_recovery_request_at"] = _utc_now()
            task_entry["last_dirty_recovery_request_source"] = source
        _update_state(remember)

        ensured = _ensure_board_governor(None, case, effective_board, policy)
        _append_event(
            "dirty_checkpoint_recovery_event_requested",
            task_id=tid,
            case_id=case_id,
            source=source,
            target_status=status,
            workspace=checkpoint.get("path"),
            head=checkpoint.get("head"),
            status_sha256=checkpoint.get("status_sha256"),
            result=ensured,
        )
        return {
            "ok": bool(ensured.get("ok")),
            "case_id": case_id,
            "created": True,
            "checkpoint": checkpoint,
            "governor": ensured,
            "retry_authorized": False,
            "target_reactivated": False,
        }
    except GovernanceStateError as exc:
        _append_event(
            "dirty_checkpoint_recovery_event_failed",
            task_id=tid,
            source=source,
            fail_closed=True,
            error=str(exc)[:1000],
        )
        return {"ok": False, "error": "STATE_UNAVAILABLE", "detail": str(exc)[:1000]}
    except Exception as exc:
        _append_event(
            "dirty_checkpoint_recovery_event_failed",
            task_id=tid,
            source=source,
            fail_closed=True,
            error=f"{type(exc).__name__}: {exc}"[:1000],
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}


def _dirty_recovery_guard_comment_present(kb, conn, task_id: str) -> bool:
    try:
        comments = list(kb.list_comments(conn, task_id) or [])
    except Exception:
        return False
    for comment in reversed(comments[-20:]):
        author = str(getattr(comment, "author", "") or "")
        body = str(getattr(comment, "body", "") or "")
        if (
            author == "retry-checkpoint-guard"
            and "WORKTREE_RETRY_CHECKPOINT_GUARD" in body
            and "automatic_redispatch: refused" in body
        ):
            return True
    return False


def _dirty_recovery_catchup_scan(board: str | None) -> dict[str, Any]:
    """Bounded restart/catch-up scan for already-durable retry guard blocks."""
    global _DIRTY_RECOVERY_LAST_CATCHUP_MONOTONIC
    policy = _load_policy()
    cfg = _dirty_recovery_cfg(policy)
    if not bool(cfg.get("catchup_scan_enabled", True)):
        return {"ok": True, "skipped": "disabled"}
    interval = max(10, int(cfg.get("catchup_scan_interval_seconds") or 60))
    now_mono = time.monotonic()
    if now_mono - _DIRTY_RECOVERY_LAST_CATCHUP_MONOTONIC < interval:
        return {"ok": True, "skipped": "interval"}
    _DIRTY_RECOVERY_LAST_CATCHUP_MONOTONIC = now_mono

    effective_board = _normalized_board(board)
    max_tasks = max(1, min(1000, int(cfg.get("catchup_max_tasks") or 200)))
    candidates: list[str] = []
    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=effective_board)
        try:
            for task in list(kb.list_tasks(conn, limit=max_tasks) or []):
                status = str(getattr(task, "status", "") or "")
                profile = str(getattr(task, "assignee", "") or "")
                if status not in {"blocked", "triage"}:
                    continue
                if profile not in DIRTY_RECOVERY_ALLOWED_TARGET_PROFILES:
                    continue
                if getattr(task, "current_run_id", None) is not None:
                    continue
                tid = str(getattr(task, "id", "") or "")
                if tid and _dirty_recovery_guard_comment_present(kb, conn, tid):
                    candidates.append(tid)
        finally:
            conn.close()
    except Exception as exc:
        _append_event(
            "dirty_checkpoint_recovery_catchup_failed",
            board=effective_board,
            error=f"{type(exc).__name__}: {exc}"[:1000],
        )
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:1000]}

    results = []
    for tid in candidates:
        result = _ensure_dirty_recovery_case_for_task(
            tid,
            effective_board,
            reason="durable retry-checkpoint-guard catch-up after dispatcher restart",
            source="retry_checkpoint_guard_catchup",
        )
        results.append({"task_id": tid, "result": result})

    if candidates:
        _append_event(
            "dirty_checkpoint_recovery_catchup_scanned",
            board=effective_board,
            candidates=candidates,
            results=results,
        )
    return {"ok": True, "candidates": candidates, "results": results}

def _decision_packet(case: dict[str, Any]) -> str:
    progress = case.get("progress") or {}
    err = case.get("last_api_error") or {}
    circuit = case.get("provider_circuit") or {}
    return (
        f"case={case.get('case_id')} task={case.get('task_id')} "
        f"attempts={case.get('total_attempts')}/{case.get('max_total_attempts')} "
        f"progress={progress.get('material_progress')} "
        f"progress_reason={progress.get('reason') or '-'} "
        f"error={err.get('reason') or '-'} "
        f"status={err.get('status_code') if err else '-'} "
        f"provider={err.get('provider') or '-'} "
        f"circuit={circuit.get('state') or 'CLOSED'}"
    )



def _governance_board_state(state: dict[str, Any], board: str) -> dict[str, Any]:
    boards = state.setdefault("governance_boards", {})
    return boards.setdefault(
        _normalized_board(board),
        {
            "governor_task_id": None,
            "pending_cases": [],
            "active_case_id": None,
            "active_run_id": None,
            "lifecycle": "UNINITIALIZED",
            "last_case_id": None,
        },
    )


def _enqueue_governance_case(case: dict[str, Any], board: str) -> dict[str, Any]:
    """Durably enqueue one immutable case on the board-level Governor."""
    effective_board = _normalized_board(board)
    holder: dict[str, Any] = {}

    def mutate(state):
        entry = _governance_board_state(state, effective_board)
        queue = entry.setdefault("pending_cases", [])
        case_id = str(case["case_id"])
        active = str(entry.get("active_case_id") or "")
        already = case_id == active or case_id in queue
        if not already:
            queue.append(case_id)
        prior = str(entry.get("lifecycle") or "UNINITIALIZED")
        # ERROR is fail-closed: a failed Governor is never resurrected merely
        # because another worker failed.
        if prior != "ERROR" and prior in {"UNINITIALIZED", "IDLE"}:
            entry["lifecycle"] = "QUEUED"
        holder.update(
            already=already,
            prior_lifecycle=prior,
            lifecycle=entry.get("lifecycle"),
            governor_task_id=entry.get("governor_task_id"),
            pending_cases=list(queue),
        )

    _update_state(mutate)
    return {"board": effective_board, **holder}


def _remember_board_governor(
    board: str,
    task_id: str,
    *,
    lifecycle: str | None = None,
) -> None:
    effective_board = _normalized_board(board)

    def mutate(state):
        entry = _governance_board_state(state, effective_board)
        entry["governor_task_id"] = str(task_id)
        if lifecycle and str(entry.get("lifecycle") or "") != "ERROR":
            entry["lifecycle"] = lifecycle
        task = state.setdefault("tasks", {}).setdefault(
            str(task_id), {"total_attempts": 0, "runs": {}}
        )
        task["board_hint"] = effective_board
        task["governance_board"] = effective_board

    _update_state(mutate)


def _mark_board_governor_error(board: str, task_id: str, case_id: str | None = None) -> None:
    effective_board = _normalized_board(board)

    def mutate(state):
        entry = _governance_board_state(state, effective_board)
        if not entry.get("governor_task_id"):
            entry["governor_task_id"] = str(task_id)
        if str(entry.get("governor_task_id") or "") == str(task_id):
            entry["lifecycle"] = "ERROR"
            entry["last_error_case_id"] = case_id
            entry["last_error_at"] = _utc_now()

    _update_state(mutate)


def _reopen_completed_governor_card(
    task_id: str,
    board: str,
    *,
    reason: str,
) -> dict[str, Any]:
    """CAS-reopen the singleton Governor from done -> ready.

    Hermes v0.20.2 has no public reopen verb; its dashboard performs direct
    status transitions for reopen operations. This narrow write only accepts a
    fully completed task with no current run, clears terminal presentation
    fields, and appends a task event in the same transaction.
    """
    effective_board = _normalized_board(board)
    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=effective_board)
        try:
            task = kb.get_task(conn, task_id)
            if task is None:
                return {"ok": False, "error": "governor_task_not_found", "board": effective_board}
            status = str(getattr(task, "status", "") or "")
            if status in {"ready", "running"}:
                return {"ok": True, "method": "already_dispatchable", "status": status, "board": effective_board}
            if status != "done":
                return {
                    "ok": False,
                    "error": "governor_not_safely_reopenable",
                    "status": status,
                    "board": effective_board,
                }
            if getattr(task, "current_run_id", None) is not None:
                return {
                    "ok": False,
                    "error": "governor_done_with_current_run",
                    "board": effective_board,
                }

            now = int(time.time())
            payload = json.dumps(
                {
                    "from": "done",
                    "to": "ready",
                    "reason": reason,
                    "source": "execution-governance",
                },
                ensure_ascii=False,
            )
            with kb.write_txn(conn):
                cur = conn.execute(
                    """
                    UPDATE tasks
                    SET status = 'ready',
                        completed_at = NULL,
                        result = NULL
                    WHERE id = ?
                      AND status = 'done'
                      AND current_run_id IS NULL
                    """,
                    (task_id,),
                )
                if cur.rowcount != 1:
                    return {
                        "ok": False,
                        "error": "governor_reopen_cas_lost",
                        "board": effective_board,
                    }
                # Hermes 0.20.x recent_success respawn guard recognizes an
                # explicit post-completion status->ready event as deliberate
                # requeue intent. Keep our governance-specific audit event too.
                status_payload = json.dumps(
                    {
                        "status": "ready",
                        "from": "done",
                        "reason": reason,
                        "source": "execution-governance",
                    },
                    ensure_ascii=False,
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, ?, ?, ?)",
                    (task_id, "status", status_payload, now),
                )
                conn.execute(
                    "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                    "VALUES (?, NULL, ?, ?, ?)",
                    (task_id, "governance_requeued", payload, now),
                )
            try:
                kb.notify_task_updated(
                    conn,
                    task_id,
                    ["status", "completed_at", "result"],
                    board=effective_board,
                )
            except Exception:
                pass
            return {
                "ok": True,
                "method": "cas_done_to_ready",
                "status": "ready",
                "board": effective_board,
            }
        finally:
            conn.close()
    except Exception as exc:
        return {
            "ok": False,
            "error": f"{type(exc).__name__}:{str(exc)[:900]}",
            "board": effective_board,
        }



# HERMES_DIRTY_CHECKPOINT_RECOVERY_V2_2026_09_04: fail-closed stale Governor reconciliation.
def _reconciliation_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    value = policy.get("execution_governor_reconciliation") or {}
    return value if isinstance(value, dict) else {}


def _case_epoch(case: dict[str, Any]) -> float | None:
    raw = str(case.get("created_at") or "").strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _load_case_for_reconcile(case_id: str) -> dict[str, Any] | None:
    try:
        path = _governance_dir() / "cases" / f"{case_id}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _quarantine_stale_case(case_id: str, *, reason: str, board: str) -> bool:
    case = _load_case_for_reconcile(case_id)
    if not isinstance(case, dict):
        return False
    if str(case.get("status") or "") != "PENDING":
        return True
    case["status"] = "STALE"
    case["stale"] = {
        "quarantined_at": _utc_now(),
        "reason": reason,
        "board": board,
        "preserved": True,
    }
    _write_case(case)
    return True


def _reconcile_error_governor(board: str, policy: dict[str, Any]) -> dict[str, Any]:
    """Repair only a provably inactive ERROR singleton; never steal live work."""
    effective_board = _normalized_board(board)
    cfg = _reconciliation_cfg(policy)
    if not bool(cfg.get("enabled", True)):
        return {"ok": False, "skipped": "reconciliation_disabled", "board": effective_board}

    with _file_lock(_state_path()):
        snapshot = _read_state_unlocked()
    entry = ((snapshot.get("governance_boards") or {}).get(effective_board) or {})
    if str(entry.get("lifecycle") or "") != "ERROR":
        return {"ok": True, "needed": False, "board": effective_board}

    governor_task_id = str(entry.get("governor_task_id") or "").strip()
    governor_task = None
    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=effective_board)
        try:
            governor_task = kb.get_task(conn, governor_task_id) if governor_task_id else None
        finally:
            conn.close()
    except Exception as exc:
        return {"ok": False, "error": f"governor_lookup_failed:{type(exc).__name__}:{exc}"[:900]}

    if governor_task is not None:
        current_run = getattr(governor_task, "current_run_id", None)
        status = str(getattr(governor_task, "status", "") or "")
        if current_run is not None or status in {"running", "ready"}:
            return {
                "ok": False,
                "error": "governor_may_still_be_active",
                "governor_task_id": governor_task_id,
                "status": status,
                "current_run_id": current_run,
            }
        if status != "done":
            return {
                "ok": False,
                "error": "existing_error_governor_requires_human",
                "governor_task_id": governor_task_id,
                "status": status,
            }
    elif governor_task_id and not bool(cfg.get("rebuild_when_governor_missing", True)):
        return {"ok": False, "error": "governor_missing_rebuild_disabled"}

    ttl_hours = max(1, int(cfg.get("stale_pending_case_ttl_hours") or 24))
    ttl_seconds = ttl_hours * 3600
    now = time.time()
    stale_ids: list[str] = []
    recent_pending: list[str] = []
    unreadable_pending: list[str] = []

    # Reconcile every PENDING case known to this board, not only queue entries.
    for case_id, meta in (snapshot.get("cases") or {}).items():
        if not isinstance(meta, dict) or str(meta.get("status") or "") != "PENDING":
            continue
        case = _load_case_for_reconcile(str(case_id))
        if not isinstance(case, dict):
            unreadable_pending.append(str(case_id))
            continue
        if _normalized_board(case.get("board")) != effective_board:
            continue
        created = _case_epoch(case)
        if created is None:
            unreadable_pending.append(str(case_id))
            continue
        if now - created >= ttl_seconds:
            stale_ids.append(str(case_id))
        else:
            recent_pending.append(str(case_id))

    active_case = str(entry.get("active_case_id") or "").strip()
    if active_case and active_case in unreadable_pending:
        return {"ok": False, "error": "active_case_unreadable", "active_case_id": active_case}
    if active_case and active_case in recent_pending:
        return {
            "ok": False,
            "error": "recent_active_case_requires_human",
            "active_case_id": active_case,
            "ttl_hours": ttl_hours,
        }
    if unreadable_pending:
        return {
            "ok": False,
            "error": "pending_case_unreadable",
            "case_ids": unreadable_pending[:20],
        }

    for case_id in stale_ids:
        if not _quarantine_stale_case(
            case_id,
            reason=f"ERROR singleton reconciliation after >= {ttl_hours}h pending",
            board=effective_board,
        ):
            return {"ok": False, "error": "stale_case_quarantine_failed", "case_id": case_id}

    def mutate(state):
        board_entry = _governance_board_state(state, effective_board)
        for case_id in stale_ids:
            meta = (state.get("cases") or {}).get(case_id)
            if isinstance(meta, dict):
                meta["status"] = "STALE"
                meta["stale_at"] = _utc_now()
        board_entry["pending_cases"] = [
            str(cid) for cid in (board_entry.get("pending_cases") or [])
            if str(cid) not in set(stale_ids)
        ]
        if str(board_entry.get("active_case_id") or "") in set(stale_ids):
            board_entry["active_case_id"] = None
            board_entry["active_run_id"] = None
        if governor_task is None:
            board_entry["governor_task_id"] = None
        remaining_queue = list(board_entry.get("pending_cases") or [])
        if remaining_queue:
            board_entry["lifecycle"] = "QUEUED"
        elif governor_task is not None and str(getattr(governor_task, "status", "") or "") == "done":
            board_entry["lifecycle"] = "IDLE"
        else:
            board_entry["lifecycle"] = "UNINITIALIZED"
        board_entry["last_reconciled_at"] = _utc_now()
        board_entry["last_reconciled_stale_cases"] = list(stale_ids)
    _update_state(mutate)

    result = {
        "ok": True,
        "needed": True,
        "board": effective_board,
        "prior_governor_task_id": governor_task_id or None,
        "governor_missing": governor_task is None,
        "stale_cases": stale_ids,
        "recent_pending_cases": recent_pending,
        "ttl_hours": ttl_hours,
    }
    _append_event("execution_governor_stale_reconciled", **result)
    return result

def _ensure_board_governor(
    ctx,
    case: dict[str, Any],
    board: str | None,
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Enqueue *case* and ensure exactly one reusable Governor card per board."""
    del ctx
    actions = policy.get("kanban_actions") or {}
    cfg = actions.get("governance_cards") or {}
    tenant = case.get("tenant")
    if not _auto_create_allowed(actions, board, tenant):
        return {
            "ok": False,
            "skipped": "auto_create_scope_denied",
            "board": _normalized_board(board),
            "tenant": tenant,
        }

    effective_board = _normalized_board(board)
    reconciliation = _reconcile_error_governor(effective_board, policy)
    if not reconciliation.get("ok"):
        return {
            "ok": False,
            "skipped": "governor_error_reconciliation_failed",
            "board": effective_board,
            "reconciliation": reconciliation,
        }
    conflict = _named_board_global_override_conflict(effective_board)
    if conflict:
        return {
            "ok": False,
            "skipped": "global_kanban_override_conflict",
            "board": effective_board,
            "tenant": tenant,
            **conflict,
        }

    queued = _enqueue_governance_case(case, effective_board)
    if queued.get("lifecycle") == "ERROR":
        return {
            "ok": False,
            "skipped": "governor_in_error_state",
            "board": effective_board,
            "case_id": case.get("case_id"),
            "governor_task_id": queued.get("governor_task_id"),
        }

    assignee = str(cfg.get("assignee") or "execution-governor")
    prefix = str(cfg.get("idempotency_prefix") or "execution-governance")
    idempotency_key = f"{prefix}:board:{effective_board}"
    max_runtime_seconds = int(cfg.get("max_runtime_seconds") or 300)
    max_retries = int(cfg.get("max_retries") or 1)
    title = "Execution Governor" if effective_board == "default" else f"Execution Governor [{effective_board}]"
    body = (
        "PERSISTENT BOARD-LEVEL EXECUTION GOVERNOR. "
        f"governance_board={effective_board}. "
        "Processes exactly one queued governance case per run via governance_context. "
        "When DONE it is idle; the governance guard reopens this same card when a new case arrives."
    )

    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=effective_board)
        try:
            task_id = str(queued.get("governor_task_id") or "")
            task = kb.get_task(conn, task_id) if task_id else None

            if task is None:
                task_id = kb.create_task(
                    conn,
                    title=title,
                    body=body,
                    assignee=assignee,
                    tenant=None,
                    created_by="execution-governance",
                    workspace_kind="scratch",
                    idempotency_key=idempotency_key,
                    max_runtime_seconds=max_runtime_seconds,
                    max_retries=max_retries,
                    initial_status="running",
                    board=effective_board,
                )
                task = kb.get_task(conn, task_id)
                _remember_board_governor(effective_board, task_id, lifecycle="QUEUED")
                return {
                    "ok": True,
                    "method": "kanban_db.create_task",
                    "singleton": True,
                    "task_id": task_id,
                    "status": getattr(task, "status", None),
                    "board": effective_board,
                    "case_id": case.get("case_id"),
                    "pending_cases": len(queued.get("pending_cases") or []),
                }

            task_id = str(getattr(task, "id", task_id))
            _remember_board_governor(effective_board, task_id)
            status = str(getattr(task, "status", "") or "")

            if status == "done":
                reopened = _reopen_completed_governor_card(
                    task_id,
                    effective_board,
                    reason=f"queued case {case.get('case_id')}",
                )
                if reopened.get("ok"):
                    _remember_board_governor(effective_board, task_id, lifecycle="QUEUED")
                return {
                    **reopened,
                    "singleton": True,
                    "task_id": task_id,
                    "case_id": case.get("case_id"),
                    "pending_cases": len(queued.get("pending_cases") or []),
                }

            if status in {"ready", "running"}:
                _remember_board_governor(
                    effective_board,
                    task_id,
                    lifecycle="ACTIVE" if status == "running" else "QUEUED",
                )
                return {
                    "ok": True,
                    "method": "existing_singleton",
                    "singleton": True,
                    "task_id": task_id,
                    "status": status,
                    "board": effective_board,
                    "case_id": case.get("case_id"),
                    "pending_cases": len(queued.get("pending_cases") or []),
                }

            # A blocked/triage singleton indicates a Governor failure or an
            # operator intervention. Never auto-unblock it and hide that fault.
            return {
                "ok": False,
                "skipped": "governor_not_dispatchable",
                "singleton": True,
                "task_id": task_id,
                "status": status,
                "board": effective_board,
                "case_id": case.get("case_id"),
            }
        finally:
            conn.close()
    except Exception as exc:
        return {
            "ok": False,
            "method": "board_singleton",
            "board": effective_board,
            "case_id": case.get("case_id"),
            "error": str(exc)[:1000],
        }

# HERMES_OPERATIONAL_HARDENING_2026_09_03: deterministic provider-wait lifecycle.
def _update_case_status_file(case_id: str, status: str, **metadata: Any) -> None:
    path = _governance_dir() / "cases" / f"{case_id}.json"
    try:
        with _file_lock(path):
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data["status"] = status
                if metadata:
                    data.setdefault("provider_recovery", {}).update(metadata)
                _atomic_json_write(path, data)
    except Exception as exc:
        _append_event("provider_case_file_update_failed", case_id=case_id, error=str(exc)[:800])

    def mutate(state):
        entry = (state.get("cases") or {}).get(case_id)
        if isinstance(entry, dict):
            entry["status"] = status
    try:
        _update_state(mutate)
    except Exception:
        pass


def _comment_task(board: str | None, task_id: str, body: str) -> None:
    try:
        from hermes_cli import kanban_db as kb
        conn = _kanban_connect(board=_normalized_board(board))
        try:
            kb.add_comment(conn, task_id, author="execution-governance", body=body)
        finally:
            conn.close()
    except Exception as exc:
        _append_event("provider_wait_comment_failed", task_id=task_id, error=str(exc)[:800])


def _arm_provider_wait(ctx, case: dict[str, Any], board: str | None, policy: dict[str, Any]) -> dict[str, Any] | None:
    recovery = _provider_recovery_config(policy)
    circuit = case.get("provider_circuit") or {}
    if not recovery.get("enabled") or circuit.get("state") != "OPEN":
        return None
    provider = str(circuit.get("provider") or ((case.get("last_api_error") or {}).get("provider")) or "").strip()
    if not provider:
        return None
    task_id = str(case.get("task_id") or "")
    case_id = str(case.get("case_id") or "")
    checkpoint = ((case.get("attempt") or {}).get("after") or {})
    holder: dict[str, Any] = {}

    def mutate(state):
        task = state.setdefault("tasks", {}).setdefault(task_id, {"total_attempts": 0, "runs": {}})
        retry_count = int(task.get("provider_retry_count") or 0)
        exhausted = retry_count >= int(recovery.get("max_automatic_retries") or 0)
        provider_entry = state.setdefault("providers", {}).setdefault(provider, {})
        next_retry_at = int(
            provider_entry.get("next_retry_at")
            or _next_provider_retry_at(circuit, recovery)
        )
        wait = {
            "state": "HUMAN_REQUIRED" if exhausted else "WAITING",
            "provider": provider,
            "model": circuit.get("model"),
            "case_id": case_id,
            "board": _normalized_board(board),
            "next_retry_at": None if exhausted else next_retry_at,
            "retry_count": retry_count,
            "max_automatic_retries": int(recovery.get("max_automatic_retries") or 0),
            "checkpoint": checkpoint,
            "created_at": _utc_now(),
        }
        task["provider_wait"] = wait
        task.pop("authorized_recovery_checkpoint", None)
        index = (state.get("cases") or {}).get(case_id)
        if isinstance(index, dict):
            index["status"] = "HUMAN_REQUIRED" if exhausted else "WAITING_PROVIDER"
        provider_entry["state"] = "OPEN"
        provider_entry["human_reset_required"] = exhausted
        provider_entry["next_retry_at"] = None if exhausted else next_retry_at
        holder.update(wait=wait, exhausted=exhausted)

    _update_state(mutate)
    wait = holder["wait"]
    if holder["exhausted"]:
        status = "HUMAN_REQUIRED"
        reason = (
            f"Provider recovery exhausted for {provider} after {wait['retry_count']} automatic retries. "
            f"Case {case_id} requires human intervention."
        )
        user_action = "true"
    else:
        status = "WAITING_PROVIDER"
        reason = (
            f"PROVIDER_WAIT provider={provider} case={case_id} retry_count={wait['retry_count']} "
            f"next_retry_at={wait['next_retry_at']}. No human action required."
        )
        user_action = "false"

    case["status"] = status
    case.setdefault("provider_recovery", {}).update(wait)
    _write_case(case)
    held = _hold_task(
        ctx, task_id, board, reason,
        str(((policy.get("kanban_actions") or {}).get("hold_block_kind")) or "needs_input"),
    )
    _comment_task(
        board, task_id,
        "PROVIDER_WAIT\n"
        f"case_id: {case_id}\nprovider: {provider}\nstate: {wait['state']}\n"
        f"retry_count: {wait['retry_count']}/{wait['max_automatic_retries']}\n"
        f"next_retry_at: {wait.get('next_retry_at')}\nuser_action_required: {user_action}\n",
    )
    _append_event("provider_wait_armed", task_id=task_id, case_id=case_id, provider=provider, wait=wait, hold_result=held)
    return {"handled": True, "status": status, "wait": wait, "hold_result": held}


def _revert_provider_resume(task_id: str, provider: str, case_id: str, resume_epoch: int) -> None:
    def mutate(state):
        task = (state.get("tasks") or {}).get(task_id)
        if not isinstance(task, dict):
            return
        auth = task.get("authorized_recovery_checkpoint")
        if isinstance(auth, dict) and int(auth.get("resume_epoch") or 0) == resume_epoch:
            task.pop("authorized_recovery_checkpoint", None)
        task["provider_retry_count"] = max(0, int(task.get("provider_retry_count") or 0) - 1)
        wait = task.get("provider_wait")
        if isinstance(wait, dict) and str(wait.get("case_id") or "") == case_id:
            wait["state"] = "WAITING"
        entry = (state.get("providers") or {}).get(provider)
        if isinstance(entry, dict):
            entry["state"] = "OPEN"
            entry.pop("probe_task_id", None)
            entry.pop("probe_resume_epoch", None)
    _update_state(mutate)


def _resume_due_provider_waits(board_hint: str | None = None) -> None:
    policy = _load_policy()
    recovery = _provider_recovery_config(policy)
    if not recovery.get("enabled"):
        return
    try:
        with _file_lock(_state_path()):
            snapshot = _read_state_unlocked()
    except Exception as exc:
        _append_event("provider_wait_scan_failed", error=str(exc)[:1000])
        return

    now_epoch = int(time.time())
    candidates = []
    for task_id, task in (snapshot.get("tasks") or {}).items():
        if not isinstance(task, dict):
            continue
        wait = task.get("provider_wait")
        if not isinstance(wait, dict) or wait.get("state") != "WAITING":
            continue
        due = int(wait.get("next_retry_at") or 0)
        if due <= 0 or due > now_epoch:
            continue
        candidates.append((due, str(task_id), dict(wait), dict(task)))
    candidates.sort(key=lambda item: item[0])
    probed_providers: set[str] = set()

    for _, task_id, wait_snapshot, task_snapshot in candidates:
        provider = str(wait_snapshot.get("provider") or "")
        if not provider:
            continue
        if recovery.get("one_probe_per_provider_per_dispatch_tick") and provider in probed_providers:
            continue
        provider_snapshot = (snapshot.get("providers") or {}).get(provider) or {}
        if isinstance(provider_snapshot, dict) and provider_snapshot.get("state") == "PROBING":
            continue
        probed_providers.add(provider)
        retry_count = int(task_snapshot.get("provider_retry_count") or 0)
        if retry_count >= int(recovery.get("max_automatic_retries") or 0):
            continue

        holder: dict[str, Any] = {}
        case_id = str(wait_snapshot.get("case_id") or "")
        checkpoint = wait_snapshot.get("checkpoint") or {}

        def mutate(state):
            task = (state.get("tasks") or {}).get(task_id)
            if not isinstance(task, dict):
                return
            wait = task.get("provider_wait")
            if not isinstance(wait, dict) or wait.get("state") != "WAITING":
                return
            if str(wait.get("case_id") or "") != case_id:
                return
            if int(wait.get("next_retry_at") or 0) > int(time.time()):
                return
            current_count = int(task.get("provider_retry_count") or 0)
            if current_count >= int(recovery.get("max_automatic_retries") or 0):
                wait["state"] = "HUMAN_REQUIRED"
                return
            resume_epoch = int(task.get("resume_epoch") or 0) + 1
            task["resume_epoch"] = resume_epoch
            task["provider_retry_count"] = current_count + 1
            dirty_checkpoint = (
                isinstance(checkpoint, dict)
                and checkpoint.get("available")
                and int(checkpoint.get("changed_entries") or 0) > 0
                and checkpoint.get("path")
                and checkpoint.get("head")
                and checkpoint.get("status_sha256")
            )
            if dirty_checkpoint:
                task["authorized_recovery_checkpoint"] = {
                    "state": "AUTHORIZED",
                    "reason": "provider_recovery",
                    "case_id": case_id,
                    "resume_epoch": resume_epoch,
                    "workspace": checkpoint.get("path"),
                    "head": checkpoint.get("head"),
                    "status_sha256": checkpoint.get("status_sha256"),
                    "authorized_at": int(time.time()),
                    "expires_at": int(time.time()) + int(recovery.get("authorization_ttl_seconds") or 3600),
                }
            wait["state"] = "RESUMING"
            wait["resume_epoch"] = resume_epoch
            wait["last_resume_at"] = _utc_now()
            entry = state.setdefault("providers", {}).setdefault(provider, {})
            entry["state"] = "PROBING"
            entry["probe_task_id"] = task_id
            entry["probe_resume_epoch"] = resume_epoch
            entry["probe_started_at"] = _utc_now()
            entry["human_reset_required"] = False
            index = (state.get("cases") or {}).get(case_id)
            if isinstance(index, dict):
                index["status"] = "RETRY_AUTHORIZED"
            holder.update(resume_epoch=resume_epoch, board=wait.get("board") or task.get("board_hint") or board_hint)

        _update_state(mutate)
        if not holder.get("resume_epoch"):
            continue
        board = _normalized_board(holder.get("board"))
        resume_epoch = int(holder["resume_epoch"])

        try:
            from hermes_cli import kanban_db as kb
            conn = _kanban_connect(board=board)
            try:
                task = kb.get_task(conn, task_id)
                if task is None or getattr(task, "current_run_id", None) is not None:
                    raise RuntimeError("task unavailable or still has current_run_id")
                status = str(getattr(task, "status", "") or "")
                if status == "blocked":
                    if not kb.unblock_task(conn, task_id):
                        raise RuntimeError("native unblock_task refused provider resume")
                elif status != "ready":
                    raise RuntimeError(f"task status {status} is not provider-resumable")
                kb.add_comment(
                    conn, task_id, author="execution-governance",
                    body=(
                        "PROVIDER_RETRY_AUTHORIZED\n"
                        f"case_id: {case_id}\nprovider: {provider}\nresume_epoch: {resume_epoch}\n"
                        f"automatic_retry: true\nuser_action_required: false\n"
                    ),
                )
            finally:
                conn.close()
        except Exception as exc:
            _revert_provider_resume(task_id, provider, case_id, resume_epoch)
            _append_event("provider_wait_resume_failed", task_id=task_id, case_id=case_id, provider=provider, error=str(exc)[:1000])
            continue

        _update_case_status_file(case_id, "RETRY_AUTHORIZED", resume_epoch=resume_epoch, automatic_retry=True)
        _append_event("provider_wait_resumed", task_id=task_id, case_id=case_id, provider=provider, resume_epoch=resume_epoch, board=board)


def _release_provider_probe(task_id: str, reason: str) -> None:
    holder = []
    def mutate(state):
        for provider, entry in (state.get("providers") or {}).items():
            if isinstance(entry, dict) and entry.get("state") == "PROBING" and str(entry.get("probe_task_id") or "") == task_id:
                entry["state"] = "CLOSED"
                entry["closed_at"] = _utc_now()
                entry["close_reason"] = reason
                entry.pop("probe_task_id", None)
                entry.pop("probe_resume_epoch", None)
                holder.append(provider)
    try:
        _update_state(mutate)
    except Exception:
        return
    for provider in holder:
        _append_event("provider_probe_closed", task_id=task_id, provider=provider, reason=reason)


def _on_worker_exited_factory(ctx):
    def on_worker_exited(task_id=None, assignee=None, worker_pid=None, exit_kind=None,
                         exit_code=None, outcome=None, retry_status=None, run_id=None,
                         board=None, **kwargs):
        governance_source = str(kwargs.pop("_governance_source", "worker_exit_hook"))
        del kwargs
        profile = str(assignee or "")
        if not _profile_governed(profile):
            return
        tid = str(task_id)
        rid = str(run_id) if run_id is not None else f"pid:{worker_pid}"
        case_id = f"g_{tid}_{run_id if run_id is not None else worker_pid}"
        policy = _load_policy()

        state_holder = {}
        def mutate(state):
            existing_case = (state.get("cases") or {}).get(case_id)
            if isinstance(existing_case, dict):
                state_holder["duplicate"] = True
                state_holder["case_id"] = case_id
                return

            task = state.setdefault("tasks", {}).setdefault(tid, {"total_attempts": 0, "runs": {}})
            run = task.setdefault("runs", {}).setdefault(rid, {})
            # The spawn hook is the durable source of truth for a run's board.
            # In real v0.20.2 named-board runs we observed exit callbacks carrying
            # board="default" even though the corresponding spawn callback carried
            # the correct named board. Therefore the exit-hook value is fallback-only.
            persisted_board = run.get("board")
            effective_board = persisted_board or board
            if effective_board:
                run["board"] = effective_board
            state_holder["board"] = effective_board
            state_holder["persisted_board"] = persisted_board
            state_holder["hook_board"] = board
            workspace_path = run.get("workspace_path")
            after = _git_snapshot(workspace_path)
            previous_after = None
            for other_rid, other_run in task.get("runs", {}).items():
                if other_rid == rid or not isinstance(other_run, dict):
                    continue
                if other_run.get("ended_at") and isinstance(other_run.get("after"), dict):
                    previous_after = other_run.get("after")
            run.update({
                "ended_at": _utc_now(),
                "exit_kind": exit_kind,
                "exit_code": exit_code,
                "outcome": outcome,
                "retry_status": retry_status,
                "after": after,
            })
            progress = _compute_progress(
                run.get("before"), after, int(run.get("tool_calls") or 0),
                dict(run.get("tool_counts") or {}), previous_after,
            )
            run["progress"] = progress
            provider_circuit = _latest_open_provider_for_task(state, tid)
            case = {
                "schema_version": SCHEMA_VERSION,
                "case_id": case_id,
                "status": "PENDING",
                "created_at": _utc_now(),
                "task_id": tid,
                "run_id": run_id,
                "profile": profile,
                "board": effective_board,
                "tenant": run.get("tenant"),
                "worker_pid": worker_pid,
                "exit": {
                    "kind": exit_kind,
                    "code": exit_code,
                    "outcome": outcome,
                    "retry_status": retry_status,
                },
                "total_attempts": int(task.get("total_attempts") or 0),
                "max_total_attempts": int(((policy.get("task_budget") or {}).get("default_max_total_attempts")) or 2),
                "progress": progress,
                "provider_circuit": provider_circuit,
                "last_api_error": run.get("last_api_error"),
                "attempt": run,
                "decision": None,
            }
            state.setdefault("cases", {})[case_id] = {
                "task_id": tid,
                "run_id": run_id,
                "status": "PENDING",
                "path": str(_governance_dir() / "cases" / f"{case_id}.json"),
            }
            state_holder["case"] = case

        try:
            _update_state(mutate)
        except GovernanceStateError as exc:
            # Fail closed. We have intentionally lost the canonical ledger, so
            # do not manufacture an empty ledger, case, retry, or Governor
            # dispatch. In enforce mode, hold the original task on the best
            # board information available from this lifecycle event.
            actions = policy.get("kanban_actions") or {}
            effective_board = _normalized_board(board)
            held = None
            if _enforcement_allowed(actions, effective_board, None):
                held = _hold_task(
                    ctx,
                    tid,
                    effective_board,
                    (
                        f"Execution governance STATE_UNAVAILABLE after abnormal "
                        f"run {run_id}; human repair required before retry."
                    ),
                    str(actions.get("hold_block_kind") or "needs_input"),
                )
            _append_event(
                "governance_state_unavailable",
                task_id=tid,
                run_id=run_id,
                profile=profile,
                board=effective_board,
                error=str(exc)[:1200],
                fail_closed=True,
                hold_result=held,
                source=governance_source,
            )
            return

        if state_holder.get("duplicate"):
            _append_event(
                "attempt_exit_duplicate_ignored",
                task_id=tid,
                run_id=run_id,
                case_id=state_holder.get("case_id"),
                source=governance_source,
            )
            return
        case = state_holder.get("case")
        if not case:
            return
        effective_board = state_holder.get("board") or board
        persisted_board = state_holder.get("persisted_board")
        hook_board = state_holder.get("hook_board")
        if (persisted_board and hook_board
                and _normalized_board(persisted_board) != _normalized_board(hook_board)):
            _append_event(
                "worker_exit_board_mismatch",
                task_id=tid, run_id=run_id, profile=profile,
                persisted_board=_normalized_board(persisted_board),
                hook_board=_normalized_board(hook_board),
                effective_board=_normalized_board(effective_board),
            )
        path = _write_case(case)
        _append_event("attempt_exited", task_id=tid, run_id=run_id, profile=profile,
                      board=effective_board, exit_kind=exit_kind, exit_code=exit_code,
                      outcome=outcome, retry_status=retry_status, case_id=case["case_id"],
                      progress=case["progress"], total_attempts=case["total_attempts"],
                      source=governance_source)

        actions = policy.get("kanban_actions") or {}
        enforce_this_board = _enforcement_allowed(actions, effective_board, case.get("tenant"))
        if not enforce_this_board:
            _append_event(
                "governance_case_observed",
                case_id=case["case_id"],
                path=str(path),
                board=_normalized_board(effective_board),
            )
            # Observe-mode canary: optionally create a Governor task on an explicit
            # board allowlist, but never hold/mutate the original task.
            governance_assignee = str(((actions.get("governance_cards") or {}).get("assignee")) or "execution-governor")
            if profile != governance_assignee:
                if _auto_create_allowed(actions, effective_board, case.get("tenant")):
                    created = _ensure_board_governor(ctx, case, effective_board, policy)
                    _append_event("governance_card_ensure", case_id=case["case_id"], result=created, observe_only=True)
                else:
                    cfg = actions.get("governance_cards") or {}
                    _append_event(
                        "governance_card_skipped",
                        case_id=case["case_id"],
                        reason="auto_create_scope_denied",
                        board=_normalized_board(effective_board),
                        tenant=case.get("tenant"),
                        auto_create=bool(cfg.get("auto_create", False)),
                        auto_create_boards=list(cfg.get("auto_create_boards") or []),
                        auto_create_tenants=list(cfg.get("auto_create_tenants") or []),
                    )
            return

        if case.get("provider_circuit") and _provider_recovery_config(policy).get("enabled"):
            handled = _arm_provider_wait(ctx, case, effective_board, policy)
            if handled and handled.get("handled"):
                return

        # Any probe that ended for a non-budget reason proves that the provider
        # itself is no longer the blocker; release the provider circuit and let
        # ordinary governance classify the execution failure.
        _release_provider_probe(tid, "worker_exit_without_provider_budget_error")

        # Ordinary abnormal exits are held for the Execution Governor.
        if (policy.get("task_budget") or {}).get("hold_on_first_unexpected_failure", True):
            kind = str(actions.get("hold_block_kind") or "needs_input")
            reason = (
                f"Execution governance hold after abnormal run {run_id}. "
                f"Case {case['case_id']} requires retry/human decision."
            )
            held = _hold_task(ctx, tid, effective_board, reason, kind)
            _append_event("task_governance_hold", task_id=tid, case_id=case["case_id"], result=held)

        # Do not recursively govern a failed Governor with another Governor card.
        governance_assignee = str(((actions.get("governance_cards") or {}).get("assignee")) or "execution-governor")
        if profile == governance_assignee:
            _mark_board_governor_error(effective_board, tid, case["case_id"])
            _append_event(
                "governor_failure_requires_human",
                task_id=tid,
                case_id=case["case_id"],
                board=_normalized_board(effective_board),
            )
            return

        created = _ensure_board_governor(ctx, case, effective_board, policy)
        _append_event("governance_card_ensure", case_id=case["case_id"], result=created)

    return on_worker_exited



def _mapping(value: Any) -> dict[str, Any]:
    """Best-effort normalize SQLite JSON/dataclass metadata to a mapping."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _on_dispatch_tick_factory(ctx, worker_exited_handler):
    """Bridge provider-wait wakeups and native max-runtime outcomes into governance.

    Hermes v0.20.2 emits `on_kanban_worker_exited` from crash reclamation, but
    `enforce_max_runtime()` closes a run as `timed_out` directly and exposes the
    affected task ids only through `DispatchResult.timed_out`.  The dispatch
    tick hook fires after the dispatch lock is released, so it is safe to read
    the durable run and feed the same deterministic case pipeline.
    """
    def on_dispatch_tick(result=None, board=None, **kwargs):
        del kwargs
        try:
            _resume_due_provider_waits(board)
        except Exception as exc:
            _append_event("provider_wait_tick_failed", board=_normalized_board(board), error=str(exc)[:1000])
        try:
            _dirty_recovery_catchup_scan(board)
        except Exception as exc:
            _append_event(
                "dirty_checkpoint_recovery_catchup_failed",
                board=_normalized_board(board),
                error=f"{type(exc).__name__}: {exc}"[:1000],
            )
        if result is None:
            return
        if isinstance(result, dict):
            timed_out = list(result.get("timed_out") or [])
        else:
            timed_out = list(getattr(result, "timed_out", None) or [])
        if not timed_out:
            return

        effective_board = _normalized_board(board)
        try:
            from hermes_cli import kanban_db as kb
        except Exception as exc:
            _append_event(
                "timeout_bridge_failed",
                board=effective_board,
                reason=f"kanban_import:{type(exc).__name__}:{str(exc)[:500]}",
            )
            return

        for raw_tid in timed_out:
            tid = str(raw_tid)
            try:
                conn = _kanban_connect(board=effective_board)
                try:
                    task = kb.get_task(conn, tid)
                    run = kb.latest_run(conn, tid)
                finally:
                    conn.close()

                if task is None or run is None:
                    _append_event(
                        "timeout_bridge_failed",
                        board=effective_board,
                        task_id=tid,
                        reason="task_or_latest_run_missing",
                    )
                    continue

                run_status = str(getattr(run, "status", "") or "")
                run_outcome = str(getattr(run, "outcome", "") or "")
                if run_status != "timed_out" and run_outcome != "timed_out":
                    _append_event(
                        "timeout_bridge_skipped",
                        board=effective_board,
                        task_id=tid,
                        run_id=getattr(run, "id", None),
                        run_status=run_status,
                        run_outcome=run_outcome,
                        reason="latest_run_not_timed_out",
                    )
                    continue

                metadata = _mapping(getattr(run, "metadata", None))
                retry_status = metadata.get("retry_status")
                worker_pid = metadata.get("pid")
                assignee = getattr(run, "profile", None) or getattr(task, "assignee", None)

                _append_event(
                    "timeout_bridge_observed",
                    board=effective_board,
                    task_id=tid,
                    run_id=getattr(run, "id", None),
                    profile=assignee,
                    retry_status=retry_status,
                )

                worker_exited_handler(
                    task_id=tid,
                    assignee=assignee,
                    worker_pid=worker_pid,
                    exit_kind="timed_out",
                    exit_code=None,
                    outcome="timed_out",
                    retry_status=retry_status,
                    run_id=getattr(run, "id", None),
                    board=effective_board,
                    _governance_source="dispatch_tick_timeout",
                )
            except Exception as exc:
                _append_event(
                    "timeout_bridge_failed",
                    board=effective_board,
                    task_id=tid,
                    reason=f"{type(exc).__name__}:{str(exc)[:1000]}",
                )

    return on_dispatch_tick

def _kanban_task_completed(task_id=None, run_id=None, assignee=None, **kwargs):
    del kwargs
    if not task_id:
        return
    tid = str(task_id)
    _release_provider_probe(tid, "task_completed")
    try:
        def mutate(state):
            task = (state.get("tasks") or {}).get(tid)
            if isinstance(task, dict):
                task.pop("authorized_recovery_checkpoint", None)
                task.pop("provider_wait", None)
        _update_state(mutate)
    except Exception:
        pass
    _append_event("task_completed", task_id=tid, run_id=run_id, assignee=assignee)


def _kanban_task_blocked(task_id=None, run_id=None, assignee=None, reason=None, board=None, **kwargs):
    del kwargs
    if not task_id:
        return
    reason_text = str(reason or "")
    _append_event("task_blocked", task_id=str(task_id), run_id=run_id,
                  assignee=assignee, reason=reason_text[:2000] if reason_text else None)

    # Canonical event-driven recovery trigger. Match only our deterministic
    # retry-checkpoint-guard reason; ordinary human/dependency blocks never
    # create a recovery case here.
    if reason_text.startswith("RETRY_CHECKPOINT_GUARD:"):
        result = _ensure_dirty_recovery_case_for_task(
            str(task_id),
            board,
            reason=reason_text[:500],
            source="retry_checkpoint_guard_block_hook",
        )
        _append_event(
            "dirty_checkpoint_recovery_block_hook",
            task_id=str(task_id),
            run_id=run_id,
            assignee=assignee,
            board=_normalized_board(board),
            result=result,
        )


def _governor_tool_policy(tool_name=None, **kwargs):
    del kwargs
    profile = os.environ.get("HERMES_PROFILE")
    if profile != "execution-governor":
        return None
    name = str(tool_name or "")
    if name.startswith("kanban_") or name.startswith("governance_"):
        return None
    return {
        "action": "block",
        "reason": (
            "execution-governor is restricted to Kanban and execution-governance "
            "tools. This tool is outside its governance role."
        ),
    }


def register(ctx):
    ctx.register_hook("transform_api_error_classification", _classify_api_error)
    ctx.register_hook("api_request_error", _api_request_error)
    ctx.register_hook("post_tool_call", _post_tool_call)
    ctx.register_hook("kanban_task_completed", _kanban_task_completed)
    ctx.register_hook("kanban_task_blocked", _kanban_task_blocked)
    ctx.register_hook("on_kanban_worker_spawned", _on_worker_spawned)
    worker_exited_handler = _on_worker_exited_factory(ctx)
    ctx.register_hook("on_kanban_worker_exited", worker_exited_handler)
    ctx.register_hook(
        "on_kanban_dispatch_tick",
        _on_dispatch_tick_factory(ctx, worker_exited_handler),
    )
    ctx.register_tool(
        name="request_dirty_checkpoint_recovery",
        toolset=DIRTY_RECOVERY_TOOLSET,
        schema=DIRTY_RECOVERY_SCHEMA,
        handler=_request_dirty_checkpoint_recovery,
        check_fn=_dirty_recovery_tool_available,
        emoji="🛡️",
    )
    ctx.register_hook("pre_tool_call", _governor_tool_policy)
