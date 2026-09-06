#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path

MARKER = "HERMES_DIRTY_CHECKPOINT_RECOVERY_V2_2026_09_04"
V1_MARKER = "HERMES_DIRTY_CHECKPOINT_RECOVERY_2026_09_04"

class PatchError(RuntimeError):
    pass


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise PatchError(f"Required file not found: {path}") from exc


def replace_once(text: str, old: str, new: str, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise PatchError(f"{label}: expected exactly 1 anchor, found {count}")
    return text.replace(old, new, 1)


def atomic_write(path: Path, text: str) -> None:
    st = path.stat()
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        os.fchmod(fd, stat.S_IMODE(st.st_mode))
        try:
            os.fchown(fd, st.st_uid, st.st_gid)
        except PermissionError:
            pass
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            if text and not text.endswith("\n"):
                f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def patch_version(text: str, old: str, new: str, label: str) -> str:
    if re.search(rf"(?m)^version:\s*{re.escape(new)}\s*$", text):
        return text
    pattern = rf"(?m)^version:\s*{re.escape(old)}\s*$"
    if not re.search(pattern, text):
        found = re.findall(r"(?m)^version:\s*([^\n]+)$", text)
        raise PatchError(f"{label}: expected version {old}; found {found[:1] or ['<missing>']}")
    return re.sub(pattern, f"version: {new}", text, count=1)


def patch_policy(text: str) -> str:
    if f"# {MARKER}" in text:
        return text
    if "provider_recovery:" not in text or "kanban_actions:" not in text:
        raise PatchError("policy: operational-hardening policy baseline not detected")
    anchor = "kanban_actions:\n"
    block = f'''# {MARKER}\ndirty_checkpoint_recovery:\n  event_driven: true\n  catchup_scan_enabled: true\n  catchup_scan_interval_seconds: 60\n  catchup_max_tasks: 200\nexecution_governor_reconciliation:\n  enabled: true\n  stale_pending_case_ttl_hours: 24\n  rebuild_when_governor_missing: true\n  reopen_when_governor_done: true\n\n'''
    return replace_once(text, anchor, block + anchor, "policy v2 sections")


def patch_orchestrator_soul(text: str) -> str:
    if MARKER in text:
        return text
    if V1_MARKER not in text:
        raise PatchError("orchestrator SOUL: dirty recovery v1 baseline not detected")
    addition = f'''\n\n### Event-driven dirty recovery ({MARKER})\nO caminho NORMAL de dirty-checkpoint recovery não depende de o root/orchestrator voltar a executar. Quando `retry-checkpoint-guard` persiste um bloqueio `RETRY_CHECKPOINT_GUARD`, `governance-guard` deve criar idempotentemente o case `DIRTY_CHECKPOINT_RECOVERY` e acordar o Execution Governor. `request_dirty_checkpoint_recovery` permanece somente como fallback/proativo quando o root está executável. Nunca remova dependency edges, force READY ou use `orchestration_unblock_card` para tornar o request alcançável.\n'''
    return text.rstrip() + addition + "\n"


def patch_governance_guard(text: str) -> str:
    if MARKER in text:
        return text
    if V1_MARKER not in text or "request_dirty_checkpoint_recovery" not in text:
        raise PatchError("governance-guard: dirty recovery v1 baseline not detected")

    # 1) Add deterministic event-driven case creation/catch-up helpers.
    insert_anchor = "\ndef _decision_packet(case: dict[str, Any]) -> str:\n"
    helper = r'''

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
        conn = kb.connect(board=effective_board)
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
        conn = kb.connect(board=effective_board)
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
'''
    text = replace_once(text, insert_anchor, helper + insert_anchor, "governance-guard v2 helpers")

    # 2) Reconcile a stale/missing ERROR singleton before enqueuing new work.
    ensure_anchor = "\ndef _ensure_board_governor(\n"
    reconcile_helper = r'''

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
        conn = kb.connect(board=effective_board)
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
'''
    text = replace_once(text, ensure_anchor, reconcile_helper + ensure_anchor, "governance-guard reconciliation helper")

    ensure_body_anchor = '''    effective_board = _normalized_board(board)
    conflict = _named_board_global_override_conflict(effective_board)
    if conflict:
        return {
            "ok": False,
            "skipped": "global_kanban_override_conflict",
'''
    ensure_body_new = '''    effective_board = _normalized_board(board)
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
'''
    text = replace_once(text, ensure_body_anchor, ensure_body_new, "governance-guard ensure reconciliation")

    # 3) React directly to the durable retry-guard block hook.
    blocked_anchor = '''def _kanban_task_blocked(task_id=None, run_id=None, assignee=None, reason=None, **kwargs):
    del kwargs
    if not task_id:
        return
    _append_event("task_blocked", task_id=str(task_id), run_id=run_id,
                  assignee=assignee, reason=str(reason)[:2000] if reason else None)
'''
    blocked_new = '''def _kanban_task_blocked(task_id=None, run_id=None, assignee=None, reason=None, board=None, **kwargs):
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
'''
    text = replace_once(text, blocked_anchor, blocked_new, "governance-guard blocked hook")

    # 4) Catch up already-durable pre-v2 blocks after restart (including the current degraded smoke).
    # The 2026-09-03 operational hardening already inserted provider-wait
    # recovery at the beginning of this dispatch callback. Anchor on that
    # post-hardening shape so v2 is applied to the real live baseline.
    tick_anchor = '''    def on_dispatch_tick(result=None, board=None, **kwargs):
        del kwargs
        try:
            _resume_due_provider_waits(board)
        except Exception as exc:
            _append_event("provider_wait_tick_failed", board=_normalized_board(board), error=str(exc)[:1000])
        if result is None:
            return
'''
    tick_new = '''    def on_dispatch_tick(result=None, board=None, **kwargs):
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
'''
    text = replace_once(text, tick_anchor, tick_new, "governance-guard dispatch catchup")

    return text


def sha10(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="Hermes Dirty Checkpoint Recovery v2 patch")
    parser.add_argument("--root", default="/opt/data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve(strict=False)
    files = {
        "governance_guard": root / "plugins/governance-guard/__init__.py",
        "governance_guard_manifest": root / "plugins/governance-guard/plugin.yaml",
        "policy": root / "profiles/execution-governor/governance/policy.yaml",
        "orchestrator_soul": root / "profiles/implementation-orchestrator/SOUL.md",
    }
    originals = {name: read_text(path) for name, path in files.items()}
    patched = dict(originals)
    patched["governance_guard"] = patch_governance_guard(originals["governance_guard"])
    patched["governance_guard_manifest"] = patch_version(
        originals["governance_guard_manifest"], "0.3.1", "0.3.2", "governance-guard manifest"
    )
    patched["policy"] = patch_policy(originals["policy"])
    patched["orchestrator_soul"] = patch_orchestrator_soul(originals["orchestrator_soul"])

    changed = [name for name in files if patched[name] != originals[name]]
    print(f"Root: {root}")
    print(f"Files requiring changes: {len(changed)}")
    for name in changed:
        rel = files[name].relative_to(root)
        print(f"  {rel}  {sha10(originals[name])} -> {sha10(patched[name])}")
    if args.dry_run:
        print("DRY RUN: no files written.")
        return 0
    if not changed:
        print("Already applied; no files written.")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup = root / f".dirty-checkpoint-recovery-v2-backup-{stamp}"
    backup.mkdir(parents=True, exist_ok=False)
    try:
        for name in changed:
            rel = files[name].relative_to(root)
            dest = backup / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(files[name], dest)
        for name in changed:
            atomic_write(files[name], patched[name])
    except Exception:
        # Fail closed and restore anything already written.
        for name in changed:
            rel = files[name].relative_to(root)
            src = backup / rel
            if src.exists():
                shutil.copy2(src, files[name])
        raise

    print(f"Applied successfully. Backup: {backup}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"PATCH ERROR: {exc}")
        raise SystemExit(2)
