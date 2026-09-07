"""Exclusive execution-governor tools.

v0.2.9 fails closed when canonical state is unreadable/corrupt and preserves file metadata on atomic writes.\n\nv0.2.8.1 keeps one persistent Execution Governor card per Kanban board.
Each Governor run atomically claims exactly one pending immutable case from the
board queue, applies one decision, completes normally, and is reopened by the
guard only when another case is queued.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

class GovernanceStateError(RuntimeError):
    """Canonical governance state could not be read safely."""



def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home()).resolve(strict=False)
    except Exception:
        home = Path(os.environ.get("HERMES_HOME", ".")).resolve(strict=False)
    root = home / "governance"
    root.mkdir(parents=True, exist_ok=True)
    (root / "cases").mkdir(parents=True, exist_ok=True)
    return root


@contextmanager
def _lock(path: Path):
    lock = path.with_name(f".{path.name}.lock")
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


def _replacement_metadata(path: Path) -> tuple[int, int, int]:
    try:
        st = path.stat()
        return st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)
    except FileNotFoundError:
        parent = path.parent.stat()
        return parent.st_uid, parent.st_gid, 0o600


def _apply_fd_metadata(fd: int, path: Path) -> None:
    uid, gid, mode = _replacement_metadata(path)
    os.fchmod(fd, mode)
    current = os.fstat(fd)
    if current.st_uid != uid or current.st_gid != gid:
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


def _atomic_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        _apply_fd_metadata(fd, path)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        _fsync_parent(path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _policy() -> dict[str, Any]:
    try:
        value = yaml.safe_load((_root() / "policy.yaml").read_text(encoding="utf-8")) or {}
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _default_state() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "providers": {},
        "tasks": {},
        "cases": {},
        "governance_boards": {},
    }


def _normalize_state(value: Any, path: Path) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GovernanceStateError(f"STATE_UNAVAILABLE: {path} root is not a JSON object")
    value.setdefault("schema_version", 1)
    value.setdefault("providers", {})
    value.setdefault("tasks", {})
    value.setdefault("cases", {})
    value.setdefault("governance_boards", {})
    return value


def _read_state_path(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
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


def _state() -> dict[str, Any]:
    return _read_state_path(_root() / "state.json")


def _append_event(event: str, **payload: Any) -> None:
    path = _root() / "events.jsonl"
    with _lock(path):
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": _now(), "event": event, **payload}, ensure_ascii=False, sort_keys=True) + "\n")


def _case_path(case_id: str) -> Path:
    safe = str(case_id).strip()
    if not safe or "/" in safe or "\\" in safe or ".." in safe:
        raise ValueError("invalid case_id")
    return _root() / "cases" / f"{safe}.json"


def _read_case(case_id: str) -> dict[str, Any]:
    value = json.loads(_case_path(case_id).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("case root must be an object")
    return value


def _write_case(case: dict[str, Any]) -> None:
    path = _case_path(str(case["case_id"]))
    with _lock(path):
        _atomic_json(path, case)


def _update_state_case(case: dict[str, Any]) -> None:
    path = _root() / "state.json"
    with _lock(path):
        state = _read_state_path(path)
        state.setdefault("schema_version", 1)
        state.setdefault("providers", {})
        state.setdefault("tasks", {})
        state.setdefault("governance_boards", {})
        state.setdefault("cases", {})[case["case_id"]] = {
            "task_id": case.get("task_id"), "run_id": case.get("run_id"),
            "status": case.get("status"), "path": str(_case_path(str(case["case_id"]))),
        }
        state["last_updated_at"] = _now()
        _atomic_json(path, state)


def _ok(**kwargs) -> str:
    return json.dumps({"ok": True, **kwargs}, ensure_ascii=False)


def _err(message: str, **kwargs) -> str:
    return json.dumps({"ok": False, "error": message, **kwargs}, ensure_ascii=False)


def _norm(value: Any) -> str:
    return str(value or "").strip()


def _in_list(value: str | None, values: Any, default_if_empty: bool = False) -> bool:
    if not isinstance(values, (list, tuple, set)):
        return default_if_empty
    wanted = {_norm(v) for v in values if _norm(v)}
    if not wanted:
        return default_if_empty
    return "*" in wanted or _norm(value) in wanted


def _mutation_allowed(case: dict[str, Any], policy: dict[str, Any]) -> tuple[bool, str]:
    actions = policy.get("kanban_actions") or {}
    mode = _norm(actions.get("mode") or "observe").lower()
    if mode != "enforce":
        return False, f"mode={mode}"
    board = _norm(case.get("board") or "default") or "default"
    tenant = _norm(case.get("tenant"))
    if not _in_list(board, actions.get("enforce_boards") or [], False):
        return False, f"board={board} not allowed"
    # Empty enforce_tenants means no tenant restriction. Non-empty means exact scope.
    if not _in_list(tenant, actions.get("enforce_tenants") or [], True):
        return False, f"tenant={tenant or '<none>'} not allowed"
    return True, "allowed"


def _provider_open_for_case(case: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
    cc = case.get("provider_circuit")
    if isinstance(cc, dict) and cc.get("state") == "OPEN":
        return cc
    for provider, entry in (state.get("providers") or {}).items():
        if isinstance(entry, dict) and entry.get("state") == "OPEN" and entry.get("task_id") == case.get("task_id"):
            return {"provider": provider, **entry}
    return None


def governance_status(args: dict, **kwargs) -> str:
    del args, kwargs
    policy = _policy()
    try:
        state = _state()
    except GovernanceStateError as exc:
        return _err("STATE_UNAVAILABLE", detail=str(exc)[:1000])
    cases = state.get("cases") or {}; providers = state.get("providers") or {}
    return _ok(
        mode=((policy.get("kanban_actions") or {}).get("mode") or "observe"),
        pending_cases=sum(1 for v in cases.values() if isinstance(v, dict) and v.get("status") == "PENDING"),
        open_provider_circuits=sum(1 for v in providers.values() if isinstance(v, dict) and v.get("state") == "OPEN"),
    )


# HERMES_DIRTY_CHECKPOINT_RECOVERY_2026_09_04: revalidate the exact live Git fingerprint before authorization.
# HERMES_GIT_SNAPSHOT_TIMEOUT_2026_09_07
def _governance_git_timeout_seconds() -> int:
    try:
        raw = ((_policy().get("progress") or {}).get("git_snapshot_timeout_seconds") or 30)
        return max(5, int(raw))
    except Exception:
        return 30


def _live_retry_checkpoint(workspace: str) -> dict[str, Any]:
    path = Path(workspace).resolve(strict=False)
    if not path.is_dir():
        raise ValueError("recovery workspace does not exist")

    def run(*args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_governance_git_timeout_seconds(),
            check=False,
        )
        if proc.returncode != 0:
            raise ValueError((proc.stderr or proc.stdout or "git failed").strip()[:500])
        return proc.stdout

    head = run("rev-parse", "HEAD").strip()
    status_text = run("status", "--porcelain=v1", "--untracked-files=all")
    return {
        "workspace": str(path),
        "head": head,
        "status_sha256": hashlib.sha256(status_text.encode("utf-8")).hexdigest(),
        "changed_entries": len([line for line in status_text.splitlines() if line.strip()]),
    }


# HERMES_OPERATIONAL_HARDENING_2026_09_03: durable authorization for a preserved dirty checkpoint.
def _retry_authorization_ttl_seconds(policy: dict[str, Any]) -> int:
    cfg = policy.get("provider_recovery") or {}
    try:
        minutes = max(5, int(cfg.get("authorization_ttl_minutes") or 60))
    except Exception:
        minutes = 60
    return minutes * 60


def _authorize_retry_checkpoint(case: dict[str, Any], rationale: str) -> dict[str, Any] | None:
    attempt = case.get("attempt") or {}
    after = attempt.get("after") or {}
    if not isinstance(after, dict) or not after.get("available"):
        return None
    try:
        changed = int(after.get("changed_entries") or 0)
    except Exception:
        changed = 0
    if changed <= 0:
        return None
    workspace = _norm(after.get("path"))
    head = _norm(after.get("head"))
    status_sha256 = _norm(after.get("status_sha256"))
    if not workspace or not head or not status_sha256:
        return None

    live = _live_retry_checkpoint(workspace)
    if live.get("changed_entries", 0) <= 0:
        raise ValueError("recovery checkpoint is no longer dirty")
    if live.get("head") != head or live.get("status_sha256") != status_sha256:
        raise ValueError("recovery checkpoint fingerprint changed after governance case creation")

    policy = _policy()
    now_epoch = int(time.time())
    path = _root() / "state.json"
    with _lock(path):
        state = _read_state_path(path)
        task = state.setdefault("tasks", {}).setdefault(
            _norm(case.get("task_id")), {"total_attempts": 0, "runs": {}}
        )
        resume_epoch = int(task.get("resume_epoch") or 0) + 1
        authorization = {
            "state": "AUTHORIZED",
            "reason": (
                "dirty_checkpoint_recovery"
                if _norm(case.get("case_type")).upper() == "DIRTY_CHECKPOINT_RECOVERY"
                else "governance_retry"
            ),
            "case_id": _norm(case.get("case_id")),
            "resume_epoch": resume_epoch,
            "workspace": workspace,
            "head": head,
            "status_sha256": status_sha256,
            "authorized_at": now_epoch,
            "expires_at": now_epoch + _retry_authorization_ttl_seconds(policy),
            "rationale": rationale[:500],
        }
        task["resume_epoch"] = resume_epoch
        task["authorized_recovery_checkpoint"] = authorization
        state["last_updated_at"] = _now()
        _atomic_json(path, state)
    _append_event(
        "retry_checkpoint_authorized",
        task_id=case.get("task_id"),
        case_id=case.get("case_id"),
        resume_epoch=resume_epoch,
        workspace=workspace,
    )
    return authorization


def _revoke_retry_checkpoint(task_id: str, case_id: str) -> None:
    path = _root() / "state.json"
    with _lock(path):
        state = _read_state_path(path)
        task = (state.get("tasks") or {}).get(task_id)
        if isinstance(task, dict):
            auth = task.get("authorized_recovery_checkpoint")
            if isinstance(auth, dict) and _norm(auth.get("case_id")) == case_id:
                task.pop("authorized_recovery_checkpoint", None)
                state["last_updated_at"] = _now()
                _atomic_json(path, state)


def governance_decide(args: dict, **kwargs) -> str:
    del kwargs
    case_id = _norm(args.get("case_id"))
    decision = _norm(args.get("decision")).upper()
    rationale = _norm(args.get("rationale"))
    if not case_id or decision not in {"RETRY_AUTHORIZED", "HUMAN_REQUIRED"} or not rationale:
        return _err("case_id, decision and rationale are required")
    try:
        case = _read_case(case_id)
    except Exception as exc:
        return _err(f"cannot read case: {exc}", case_id=case_id)
    if case.get("status") != "PENDING":
        return _err("case is not pending", case_id=case_id, status=case.get("status"))

    policy = _policy()
    allowed, why = _mutation_allowed(case, policy)
    if not allowed:
        return _err("mutation denied by governance scope", case_id=case_id, reason=why)

    task_id = _norm(case.get("task_id")); board = _norm(case.get("board") or "default") or "default"
    if not task_id:
        return _err("case has no original task_id", case_id=case_id)

    if decision == "RETRY_AUTHORIZED":
        try:
            state = _state()
        except GovernanceStateError as exc:
            return _err(
                "retry denied: STATE_UNAVAILABLE",
                case_id=case_id,
                detail=str(exc)[:1000],
            )
        circuit = _provider_open_for_case(case, state)
        if circuit:
            return _err("retry denied: provider circuit OPEN", case_id=case_id)
        total = int(case.get("total_attempts") or 0)
        maximum = int(case.get("max_total_attempts") or ((policy.get("task_budget") or {}).get("default_max_total_attempts")) or 2)
        if total >= maximum:
            return _err("retry denied: attempt budget exhausted", case_id=case_id)
        if (case.get("progress") or {}).get("material_progress") is False and (policy.get("progress") or {}).get("human_required_when_no_material_progress", True):
            return _err("retry denied: no material progress", case_id=case_id)
        last_error = case.get("last_api_error") or {}
        if _norm(last_error.get("reason")) in {"model_not_found", "invalid_request", "payload_too_large"}:
            return _err("retry denied: non-retryable/configuration error", case_id=case_id)
        if _norm(case.get("case_type")).upper() == "DIRTY_CHECKPOINT_RECOVERY":
            recovery_request = case.get("recovery_request") or {}
            if not isinstance(recovery_request, dict) or recovery_request.get("validated") is not True:
                return _err("retry denied: dirty recovery request was not deterministically validated", case_id=case_id)

    retry_authorization = None
    if decision == "RETRY_AUTHORIZED":
        try:
            retry_authorization = _authorize_retry_checkpoint(case, rationale)
        except Exception as exc:
            return _err(
                "retry checkpoint authorization failed",
                case_id=case_id,
                detail=f"{type(exc).__name__}: {exc}"[:1000],
            )

    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect(board=board)
        try:
            task = kb.get_task(conn, task_id)
            if task is None:
                return _err("original task not found", case_id=case_id)
            if decision == "RETRY_AUTHORIZED":
                task_status = str(task.status)
                recovery_case = _norm(case.get("case_type")).upper() == "DIRTY_CHECKPOINT_RECOVERY"
                epoch_note = (
                    f" Resume epoch {retry_authorization.get('resume_epoch')}."
                    if isinstance(retry_authorization, dict)
                    else ""
                )
                if task_status == "blocked":
                    kb.add_comment(conn, task_id, author="execution-governor",
                                   body=f"RETRY_AUTHORIZED. Case {case_id}. {rationale}{epoch_note}")
                    if not kb.unblock_task(conn, task_id):
                        if retry_authorization:
                            _revoke_retry_checkpoint(task_id, case_id)
                        return _err("Hermes refused to unblock original task", case_id=case_id)
                elif task_status == "triage" and recovery_case and isinstance(retry_authorization, dict):
                    now = int(time.time())
                    status_payload = json.dumps(
                        {
                            "status": "ready",
                            "from": "triage",
                            "reason": f"RETRY_AUTHORIZED case {case_id}",
                            "source": "execution-governance",
                            "resume_epoch": retry_authorization.get("resume_epoch"),
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
                            (task_id,),
                        )
                        if cur.rowcount != 1:
                            _revoke_retry_checkpoint(task_id, case_id)
                            return _err("dirty recovery triage->ready CAS lost", case_id=case_id)
                        conn.execute(
                            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                            "VALUES (?, NULL, 'status', ?, ?)",
                            (task_id, status_payload, now),
                        )
                        conn.execute(
                            "INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                            "VALUES (?, NULL, 'governance_recovery_requeued', ?, ?)",
                            (task_id, status_payload, now),
                        )
                    try:
                        kb.notify_task_updated(
                            conn,
                            task_id,
                            ["status", "completed_at", "claim_lock", "claim_expires", "worker_pid", "current_run_id"],
                            board=board,
                        )
                    except Exception:
                        pass
                    kb.add_comment(conn, task_id, author="execution-governor",
                                   body=f"RETRY_AUTHORIZED. Case {case_id}. {rationale}{epoch_note} Recovered from triage under exact dirty-checkpoint authorization.")
                else:
                    if retry_authorization:
                        _revoke_retry_checkpoint(task_id, case_id)
                    return _err("original task is not safely retryable", case_id=case_id, task_status=task_status, recovery_case=recovery_case)
            else:
                kb.add_comment(conn, task_id, author="execution-governor",
                               body=f"HUMAN_REQUIRED. Case {case_id}. {rationale}")
                if str(task.status) not in {"blocked", "triage"}:
                    kb.block_task(conn, task_id,
                                  reason=f"Execution governance requires human intervention. Case {case_id}: {rationale}",
                                  kind="needs_input")
            after = kb.get_task(conn, task_id)
        finally:
            conn.close()
    except Exception as exc:
        return _err(f"governance mutation failed: {exc}", case_id=case_id)

    case["status"] = decision
    case["decision"] = {"decision": decision, "rationale": rationale, "decided_at": _now()}
    _write_case(case); _update_state_case(case)
    _append_event("governance_decision_applied", case_id=case_id, task_id=task_id,
                  decision=decision, rationale=rationale,
                  resulting_status=getattr(after, "status", None))
    return _ok(decision=decision, case_id=case_id, task_id=task_id,
               task_status=getattr(after, "status", None))



_DECISION_RE = re.compile(
    r"DECISION\s*=\s*(RETRY_AUTHORIZED|HUMAN_REQUIRED)\s*;\s*REASON\s*=\s*(.+)",
    re.IGNORECASE | re.DOTALL,
)



def _board_name() -> str:
    return _norm(os.environ.get("HERMES_KANBAN_BOARD") or "default") or "default"


def _governance_board_state(state: dict[str, Any], board: str) -> dict[str, Any]:
    boards = state.setdefault("governance_boards", {})
    return boards.setdefault(
        board,
        {
            "governor_task_id": None,
            "pending_cases": [],
            "active_case_id": None,
            "active_run_id": None,
            "lifecycle": "UNINITIALIZED",
            "last_case_id": None,
        },
    )


def _read_state_locked(path: Path) -> dict[str, Any]:
    return _read_state_path(path)


def _claim_board_case_for_worker(task_id: str, run_id: str | None) -> str | None:
    """Atomically bind one pending board case to this singleton Governor run."""
    board = _board_name()
    path = _root() / "state.json"
    holder: dict[str, Any] = {}

    with _lock(path):
        state = _read_state_locked(path)
        entry = _governance_board_state(state, board)

        # If board routing metadata was lost but this task is known as a
        # singleton on another board, recover by task id.
        if _norm(entry.get("governor_task_id")) != task_id:
            for candidate_board, candidate in (state.get("governance_boards") or {}).items():
                if isinstance(candidate, dict) and _norm(candidate.get("governor_task_id")) == task_id:
                    board = _norm(candidate_board) or board
                    entry = candidate
                    break

        if _norm(entry.get("governor_task_id")) != task_id:
            return None

        active = _norm(entry.get("active_case_id"))
        active_run = _norm(entry.get("active_run_id"))
        normalized_run = _norm(run_id)
        if active:
            if active_run == normalized_run:
                return active
            # A different run with a still-active case means the prior Governor
            # did not finalize cleanly. Fail closed; never steal the case.
            return None

        queue = list(entry.get("pending_cases") or [])
        chosen = None
        cleaned = []
        for case_id in queue:
            meta = (state.get("cases") or {}).get(case_id)
            if not isinstance(meta, dict) or meta.get("status") != "PENDING":
                continue
            if chosen is None:
                chosen = str(case_id)
            cleaned.append(str(case_id))

        entry["pending_cases"] = cleaned
        if not chosen:
            entry["lifecycle"] = "IDLE"
            state["last_updated_at"] = _now()
            _atomic_json(path, state)
            return None

        entry["active_case_id"] = chosen
        entry["active_run_id"] = normalized_run
        entry["lifecycle"] = "ACTIVE"
        task = state.setdefault("tasks", {}).setdefault(
            task_id, {"total_attempts": 0, "runs": {}}
        )
        task["governance_board"] = board
        task["governance_case_id"] = chosen
        state["last_updated_at"] = _now()
        _atomic_json(path, state)
        holder["case_id"] = chosen

    _append_event(
        "governance_case_claimed",
        governance_task_id=task_id,
        governance_run_id=run_id,
        board=board,
        case_id=holder.get("case_id"),
    )
    return holder.get("case_id")


def _release_board_case(
    task_id: str,
    run_id: str | None,
    case_id: str,
) -> tuple[bool, str]:
    """Remove the decided active case and move singleton lifecycle to FINALIZING."""
    board = _board_name()
    path = _root() / "state.json"
    with _lock(path):
        state = _read_state_locked(path)
        entry = _governance_board_state(state, board)
        if _norm(entry.get("governor_task_id")) != task_id:
            for candidate_board, candidate in (state.get("governance_boards") or {}).items():
                if isinstance(candidate, dict) and _norm(candidate.get("governor_task_id")) == task_id:
                    board = _norm(candidate_board) or board
                    entry = candidate
                    break
        if _norm(entry.get("governor_task_id")) != task_id:
            return False, "governor_task_mapping_missing"
        if _norm(entry.get("active_case_id")) != case_id:
            return False, "active_case_mismatch"
        if _norm(entry.get("active_run_id")) != _norm(run_id):
            return False, "active_run_mismatch"

        entry["pending_cases"] = [
            str(cid) for cid in (entry.get("pending_cases") or [])
            if str(cid) != case_id
        ]
        entry["last_case_id"] = case_id
        entry["active_case_id"] = None
        entry["active_run_id"] = None
        entry["lifecycle"] = "FINALIZING"
        task = state.setdefault("tasks", {}).setdefault(task_id, {"total_attempts": 0, "runs": {}})
        task.pop("governance_case_id", None)
        task["last_governance_case_id"] = case_id
        state["last_updated_at"] = _now()
        _atomic_json(path, state)
    return True, board


def _finish_board_lifecycle(task_id: str, board: str) -> bool:
    """Mark the completed singleton IDLE or QUEUED and return whether work remains."""
    path = _root() / "state.json"
    pending = False
    with _lock(path):
        state = _read_state_locked(path)
        entry = _governance_board_state(state, board)
        if _norm(entry.get("governor_task_id")) != task_id:
            return False
        pending = bool(entry.get("pending_cases"))
        entry["lifecycle"] = "QUEUED" if pending else "IDLE"
        state["last_updated_at"] = _now()
        _atomic_json(path, state)
    return pending


def _reopen_completed_governor(task_id: str, board: str, reason: str) -> tuple[bool, str]:
    """Narrow CAS done -> ready transition for the persistent singleton."""
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect(board=board)
        try:
            task = kb.get_task(conn, task_id)
            if task is None:
                return False, "governor_task_not_found"
            status = _norm(getattr(task, "status", None))
            if status in {"ready", "running"}:
                return True, status
            if status != "done" or getattr(task, "current_run_id", None) is not None:
                return False, f"not_safely_reopenable:{status}"

            import time as _time
            now = int(_time.time())
            payload = json.dumps(
                {"from": "done", "to": "ready", "reason": reason, "source": "execution-governance"},
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
                    return False, "reopen_cas_lost"
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
                    board=board,
                )
            except Exception:
                pass
            return True, "ready"
        finally:
            conn.close()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:1000]


def _governance_case_for_worker(task_id: str) -> str | None:
    """Resolve the case for this run.

    New v0.2.8.1 singleton workers atomically claim from the board queue.
    The legacy per-card mapping remains as a compatibility fallback for any
    already-created v0.2.8 Governance task that happens to run after upgrade.
    """
    run_id = _norm(os.environ.get("HERMES_KANBAN_RUN_ID"))
    claimed = _claim_board_case_for_worker(task_id, run_id)
    if claimed:
        return claimed

    entry = (_state().get("tasks") or {}).get(task_id)
    if isinstance(entry, dict):
        case_id = _norm(entry.get("governance_case_id"))
        if case_id:
            return case_id

    # Legacy recovery path for old one-case Governance cards.
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            task = kb.get_task(conn, task_id)
        finally:
            conn.close()
        if task is None:
            return None
        body = str(getattr(task, "body", "") or "")
        match = re.search(r"(?:^|\s)case=([^\s]+)", body)
        if not match:
            return None
        case_id = _norm(match.group(1))
        if not case_id or "/" in case_id or "\\" in case_id or ".." in case_id:
            return None
        if not _case_path(case_id).is_file():
            return None
        _append_event(
            "governance_case_mapping_recovered",
            governance_task_id=task_id,
            case_id=case_id,
            source="legacy_task_body",
        )
        return case_id
    except Exception:
        return None



def governance_context(args: dict, **kwargs) -> str:
    """Return the compact decision context for this Governor worker only."""
    del args, kwargs
    task_id = _norm(os.environ.get("HERMES_KANBAN_TASK"))
    if not task_id:
        return _err("not running as a governance worker")
    try:
        case_id = _governance_case_for_worker(task_id)
    except GovernanceStateError as exc:
        _append_event(
            "governance_state_unavailable",
            governance_task_id=task_id,
            fail_closed=True,
            error=str(exc)[:1200],
            source="governance_context",
        )
        return _err("STATE_UNAVAILABLE", task_id=task_id, detail=str(exc)[:1000])
    if not case_id:
        return _err("governance case mapping not found", task_id=task_id)
    try:
        case = _read_case(case_id)
    except Exception as exc:
        return _err(f"cannot read case: {exc}", case_id=case_id)

    progress = case.get("progress") or {}
    api = case.get("last_api_error") or {}
    circuit = case.get("provider_circuit") or {}
    exit_info = case.get("exit") or {}

    # Keep the payload deliberately small and decision-relevant. Exit semantics
    # are included because dispatcher timeouts/crashes may have no API error.
    recovery_request = case.get("recovery_request") or {}
    checkpoint = recovery_request.get("checkpoint") if isinstance(recovery_request, dict) else {}
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    payload = {
        "case": case_id,
        "case_type": case.get("case_type"),
        "task": case.get("task_id"),
        "attempts": case.get("total_attempts"),
        "max_attempts": case.get("max_total_attempts"),
        "progress": progress.get("material_progress"),
        "progress_reason": progress.get("reason"),
        "tool_calls": progress.get("tool_calls"),
        "exit_kind": exit_info.get("kind"),
        "outcome": exit_info.get("outcome"),
        "retry_status": exit_info.get("retry_status"),
        "error": api.get("reason"),
        "status_code": api.get("status_code"),
        "provider": api.get("provider"),
        "model": api.get("model"),
        "circuit": circuit.get("state") or "CLOSED",
        "recovery_request_validated": recovery_request.get("validated") if isinstance(recovery_request, dict) else None,
        "recovery_target_status": recovery_request.get("target_status") if isinstance(recovery_request, dict) else None,
        "checkpoint_changed_entries": checkpoint.get("changed_entries"),
    }
    _append_event(
        "governance_context_read",
        governance_task_id=task_id,
        case_id=case_id,
    )
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))



def _complete_governance_worker(
    task_id: str,
    run_id: int | None,
    case_id: str,
    decision: str,
    rationale: str,
    mode: str,
) -> tuple[bool, str]:
    """Complete one singleton Governor run through the Kanban kernel."""
    try:
        from hermes_cli import kanban_db as kb
        conn = kb.connect()
        try:
            summary = f"DECISION={decision}; REASON={rationale}"
            ok = kb.complete_task(
                conn,
                task_id,
                result=summary,
                summary=summary,
                metadata={
                    "governance_case_id": case_id,
                    "governance_decision": decision,
                    "governance_mode": mode,
                    "governance_singleton": True,
                },
                expected_run_id=run_id,
            )
            if not ok:
                task = kb.get_task(conn, task_id)
                durable_status = getattr(task, "status", None) if task else None
                durable_run = getattr(task, "current_run_id", None) if task else None
                return False, (
                    "kanban_complete_refused "
                    f"status={durable_status} current_run_id={durable_run} "
                    f"expected_run_id={run_id}"
                )
            return True, summary
        finally:
            conn.close()
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"[:1000]


def _finalize_governance_output(response_text=None, **kwargs):
    """Convert one LLM decision line into deterministic lifecycle actions.

    Runs only inside dispatcher-owned execution-governor workers. The LLM does
    not receive Kanban/governance mutation tools; this hook is the trusted
    boundary that validates its output, applies policy, and closes the worker.
    """
    del kwargs
    task_id = _norm(os.environ.get("HERMES_KANBAN_TASK"))
    if not task_id:
        return None

    text = _norm(response_text)
    match = _DECISION_RE.search(text)
    if not match:
        _append_event(
            "governance_output_invalid",
            task_id=task_id,
            response_preview=text[:300],
        )
        # Fail closed: leave the run un-terminated so the dispatcher records
        # the Governor failure. Never infer a decision from malformed output.
        return None

    decision = match.group(1).upper()
    rationale = " ".join(match.group(2).strip().split())
    rationale = rationale[:500]
    if not rationale:
        _append_event("governance_output_invalid", task_id=task_id, reason="empty_rationale")
        return None

    try:
        case_id = _governance_case_for_worker(task_id)
    except GovernanceStateError as exc:
        _append_event(
            "governance_state_unavailable",
            governance_task_id=task_id,
            fail_closed=True,
            error=str(exc)[:1200],
            source="finalize_governance_output",
        )
        return None
    if not case_id:
        _append_event("governance_output_invalid", task_id=task_id, reason="missing_governance_case_id")
        return None

    mode = _norm(((_policy().get("kanban_actions") or {}).get("mode")) or "observe").lower()
    applied = None
    if mode == "enforce":
        try:
            applied = json.loads(
                governance_decide(
                    {
                        "case_id": case_id,
                        "decision": decision,
                        "rationale": rationale,
                    }
                )
            )
        except Exception as exc:
            applied = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(applied, dict) or not applied.get("ok"):
            _append_event(
                "governance_worker_finalize_failed",
                task_id=task_id,
                case_id=case_id,
                decision=decision,
                stage="apply_decision",
                result=applied,
            )
            return None

    raw_run_id = _norm(os.environ.get("HERMES_KANBAN_RUN_ID"))
    try:
        run_id = int(raw_run_id) if raw_run_id else None
    except ValueError:
        run_id = None

    try:
        released, board_or_error = _release_board_case(
            task_id, raw_run_id, case_id
        )
    except GovernanceStateError as exc:
        _append_event(
            "governance_state_unavailable",
            governance_task_id=task_id,
            case_id=case_id,
            fail_closed=True,
            error=str(exc)[:1200],
            source="release_board_case",
        )
        return None
    singleton_mode = released
    board = board_or_error if released else _board_name()

    # Legacy one-case Governance cards created before v0.2.8.1 do not have a
    # board-singleton queue mapping. Keep their old completion behavior.
    if not released and board_or_error != "governor_task_mapping_missing":
        _append_event(
            "governance_worker_finalize_failed",
            task_id=task_id,
            case_id=case_id,
            decision=decision,
            stage="release_governance_case",
            error=board_or_error,
        )
        return None

    ok, detail = _complete_governance_worker(
        task_id, run_id, case_id, decision, rationale, mode
    )
    if not ok:
        _append_event(
            "governance_worker_finalize_failed",
            task_id=task_id,
            case_id=case_id,
            decision=decision,
            stage="complete_governance_card",
            error=detail,
        )
        return None

    pending = False
    requeue_detail = None
    if singleton_mode:
        pending = _finish_board_lifecycle(task_id, board)
        if pending:
            reopened, requeue_detail = _reopen_completed_governor(
                task_id,
                board,
                reason=f"pending governance cases after {case_id}",
            )
            if not reopened:
                _append_event(
                    "governance_worker_finalize_failed",
                    task_id=task_id,
                    case_id=case_id,
                    decision=decision,
                    stage="requeue_singleton_governor",
                    error=requeue_detail,
                )
                return None

    _append_event(
        "governance_worker_finalized",
        task_id=task_id,
        case_id=case_id,
        decision=decision,
        rationale=rationale,
        mode=mode,
        run_id=run_id,
        singleton=singleton_mode,
        pending_cases=bool(pending),
        requeue=requeue_detail,
    )
    return detail


SCHEMAS = {
    "governance_context": {
        "name": "governance_context",
        "description": "Read the compact execution-governance case for this worker. Call once before deciding.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "governance_status": {
        "name": "governance_status",
        "description": "Read governance mode and pending/open counts.",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
    "governance_decide": {
        "name": "governance_decide",
        "description": "In enforce mode, atomically apply RETRY_AUTHORIZED or HUMAN_REQUIRED to one case.",
        "parameters": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["RETRY_AUTHORIZED", "HUMAN_REQUIRED"]},
                "rationale": {"type": "string"},
            },
            "required": ["case_id", "decision", "rationale"],
        },
    },
}

HANDLERS = {"governance_context": governance_context, "governance_status": governance_status, "governance_decide": governance_decide}


def register(ctx):
    if ctx.profile_name != "execution-governor":
        return

    is_worker = bool(os.environ.get("HERMES_KANBAN_TASK"))
    if is_worker:
        # Specialized Governor workers do not terminate via model-facing
        # kanban_complete/kanban_block. The trusted transform hook owns their
        # lifecycle, so the generic Kanban stop nudge would be a false protocol
        # violation and would consume an extra model turn.
        os.environ["HERMES_KANBAN_STOP_NUDGE"] = "0"

        # The dispatcher prompt contains only the task id. Expose exactly one
        # tiny read-only context tool so the model can obtain the decision facts
        # without receiving the full Kanban tool surface.
        names = ("governance_context",)
        ctx.register_hook("transform_llm_output", _finalize_governance_output)
    else:
        # Manual diagnostics remain available outside dispatcher workers.
        names = ("governance_status", "governance_decide")

    for name in names:
        schema = SCHEMAS[name]
        ctx.register_tool(name=name, toolset="execution_governance", schema=schema,
                          handler=HANDLERS[name], description=schema["description"])
