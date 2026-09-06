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

MARKER = "HERMES_DIRTY_CHECKPOINT_RECOVERY_2026_09_04"
BASE_MARKER = "HERMES_OPERATIONAL_HARDENING_2026_09_03"

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


def patch_governance_guard(text: str) -> str:
    if MARKER in text:
        return text
    if BASE_MARKER not in text or "provider_wait_resumed" not in text:
        raise PatchError("governance-guard: 2026-09-03 hardening baseline not detected")

    if "import re\n" not in text:
        text = replace_once(text, "import os\nimport subprocess\n", "import os\nimport re\nimport subprocess\n", "governance-guard import re")

    insert_anchor = "\ndef _decision_packet(case: dict[str, Any]) -> str:\n"
    helper = r'''

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
        conn = kb.connect(board=board)
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
'''
    text = replace_once(text, insert_anchor, helper + insert_anchor, "governance-guard dirty recovery helper")

    register_anchor = '''    ctx.register_hook(
        "on_kanban_dispatch_tick",
        _on_dispatch_tick_factory(ctx, worker_exited_handler),
    )
    ctx.register_hook("pre_tool_call", _governor_tool_policy)'''
    register_new = '''    ctx.register_hook(
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
    ctx.register_hook("pre_tool_call", _governor_tool_policy)'''
    text = replace_once(text, register_anchor, register_new, "governance-guard tool registration")
    return text


def patch_execution_governance(text: str) -> str:
    if MARKER in text:
        return text
    if BASE_MARKER not in text or "retry_checkpoint_authorized" not in text:
        raise PatchError("execution-governance: 2026-09-03 hardening baseline not detected")

    if "import hashlib\n" not in text:
        text = replace_once(text, "import json\n", "import hashlib\nimport json\n", "execution-governance import hashlib")
    if "import subprocess\n" not in text:
        text = replace_once(text, "import stat\n", "import stat\nimport subprocess\n", "execution-governance import subprocess")

    auth_anchor = f'''# {BASE_MARKER}: durable authorization for a preserved dirty checkpoint.\ndef _retry_authorization_ttl_seconds(policy: dict[str, Any]) -> int:\n'''
    auth_helper = r'''# HERMES_DIRTY_CHECKPOINT_RECOVERY_2026_09_04: revalidate the exact live Git fingerprint before authorization.
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
            timeout=10,
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


'''
    text = replace_once(text, auth_anchor, auth_helper + auth_anchor, "execution-governance live fingerprint helper")

    fields_anchor = '''    if not workspace or not head or not status_sha256:
        return None

    policy = _policy()
'''
    fields_new = '''    if not workspace or not head or not status_sha256:
        return None

    live = _live_retry_checkpoint(workspace)
    if live.get("changed_entries", 0) <= 0:
        raise ValueError("recovery checkpoint is no longer dirty")
    if live.get("head") != head or live.get("status_sha256") != status_sha256:
        raise ValueError("recovery checkpoint fingerprint changed after governance case creation")

    policy = _policy()
'''
    text = replace_once(text, fields_anchor, fields_new, "execution-governance exact live fingerprint")

    reason_anchor = '''            "state": "AUTHORIZED",
            "reason": "governance_retry",
            "case_id": _norm(case.get("case_id")),
'''
    reason_new = '''            "state": "AUTHORIZED",
            "reason": (
                "dirty_checkpoint_recovery"
                if _norm(case.get("case_type")).upper() == "DIRTY_CHECKPOINT_RECOVERY"
                else "governance_retry"
            ),
            "case_id": _norm(case.get("case_id")),
'''
    text = replace_once(text, reason_anchor, reason_new, "execution-governance authorization reason")

    validation_anchor = '''        last_error = case.get("last_api_error") or {}
        if _norm(last_error.get("reason")) in {"model_not_found", "invalid_request", "payload_too_large"}:
            return _err("retry denied: non-retryable/configuration error", case_id=case_id)

    retry_authorization = None
'''
    validation_new = '''        last_error = case.get("last_api_error") or {}
        if _norm(last_error.get("reason")) in {"model_not_found", "invalid_request", "payload_too_large"}:
            return _err("retry denied: non-retryable/configuration error", case_id=case_id)
        if _norm(case.get("case_type")).upper() == "DIRTY_CHECKPOINT_RECOVERY":
            recovery_request = case.get("recovery_request") or {}
            if not isinstance(recovery_request, dict) or recovery_request.get("validated") is not True:
                return _err("retry denied: dirty recovery request was not deterministically validated", case_id=case_id)

    retry_authorization = None
'''
    text = replace_once(text, validation_anchor, validation_new, "execution-governance recovery validation")

    old_branch = '''            if decision == "RETRY_AUTHORIZED":
                if str(task.status) != "blocked":
                    if retry_authorization:
                        _revoke_retry_checkpoint(task_id, case_id)
                    return _err("original task must be blocked before retry", case_id=case_id, task_status=str(task.status))
                epoch_note = (
                    f" Resume epoch {retry_authorization.get('resume_epoch')}."
                    if isinstance(retry_authorization, dict)
                    else ""
                )
                kb.add_comment(conn, task_id, author="execution-governor",
                               body=f"RETRY_AUTHORIZED. Case {case_id}. {rationale}{epoch_note}")
                if not kb.unblock_task(conn, task_id):
                    if retry_authorization:
                        _revoke_retry_checkpoint(task_id, case_id)
                    return _err("Hermes refused to unblock original task", case_id=case_id)
            else:
                kb.add_comment(conn, task_id, author="execution-governor",
                               body=f"HUMAN_REQUIRED. Case {case_id}. {rationale}")
                if str(task.status) != "blocked":
                    kb.block_task(conn, task_id,
                                  reason=f"Execution governance requires human intervention. Case {case_id}: {rationale}",
                                  kind="needs_input")
'''
    new_branch = '''            if decision == "RETRY_AUTHORIZED":
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
'''
    text = replace_once(text, old_branch, new_branch, "execution-governance blocked/triage retry branch")

    context_anchor = '''    payload = {
        "case": case_id,
        "task": case.get("task_id"),
        "attempts": case.get("total_attempts"),
'''
    context_new = '''    recovery_request = case.get("recovery_request") or {}
    checkpoint = recovery_request.get("checkpoint") if isinstance(recovery_request, dict) else {}
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    payload = {
        "case": case_id,
        "case_type": case.get("case_type"),
        "task": case.get("task_id"),
        "attempts": case.get("total_attempts"),
'''
    text = replace_once(text, context_anchor, context_new, "execution-governance context recovery header")

    circuit_anchor = '''        "model": api.get("model"),
        "circuit": circuit.get("state") or "CLOSED",
    }
'''
    circuit_new = '''        "model": api.get("model"),
        "circuit": circuit.get("state") or "CLOSED",
        "recovery_request_validated": recovery_request.get("validated") if isinstance(recovery_request, dict) else None,
        "recovery_target_status": recovery_request.get("target_status") if isinstance(recovery_request, dict) else None,
        "checkpoint_changed_entries": checkpoint.get("changed_entries"),
    }
'''
    text = replace_once(text, circuit_anchor, circuit_new, "execution-governance context recovery facts")
    return text


def patch_orchestrator_soul(text: str) -> str:
    if MARKER in text:
        return text
    if BASE_MARKER not in text:
        raise PatchError("implementation-orchestrator SOUL: hardening baseline not detected")
    anchor = "`orchestration_unblock_card` fica reservado a recovery operacional excepcional de card relacionado, nunca como parte obrigatória do gate de arquitetura. Não use CLI Kanban/SQL como workaround.\n"
    addition = anchor + f'''\n### Dirty checkpoint recovery ({MARKER})\nQuando um child relacionado estiver `blocked` ou `triage` com worktree dirty preservada e o `retry-checkpoint-guard` tiver recusado redispatch normal:\n1. NÃO chame `orchestration_unblock_card` novamente para contornar o guard;\n2. chame `request_dirty_checkpoint_recovery` exatamente uma vez com `target_task_id` e razão curta;\n3. essa operação somente valida vínculo/checkpoint, cria um case `DIRTY_CHECKPOINT_RECOVERY` e acorda o Execution Governor; ela NÃO autoriza retry e NÃO reativa o child;\n4. após request aceita, bloqueie/encerre o root por dependência e não faça polling;\n5. somente `RETRY_AUTHORIZED` do Execution Governor pode gerar `authorized_recovery_checkpoint`, incrementar `resume_epoch` e reativar o mesmo child;\n6. preserve o WIP; nunca use `reset`, `clean`, delete, reprovisionamento ou nova worktree para contornar o checkpoint.\n\nSe o request falhar por fingerprint/state/vínculo, preserve tudo e trate como `BLOCKED_OPERATIONAL`; não fabrique state.json nem autorização manual.\n'''
    return replace_once(text, anchor, addition, "orchestrator dirty recovery policy")


def patch_governor_soul(text: str) -> str:
    if MARKER in text:
        return text
    if BASE_MARKER not in text:
        raise PatchError("execution-governor SOUL: hardening baseline not detected")
    anchor = '''RETRY_AUTHORIZED only when:
- attempts remain;
- provider circuit is CLOSED;
- progress is not explicitly false; and
- the failure is transient, such as `timed_out`, timeout, temporary network failure, or HTTP 5xx.
'''
    replacement = f'''RETRY_AUTHORIZED only when:
- attempts remain;
- provider circuit is CLOSED;
- progress is not explicitly false; and
- either the failure is transient (`timed_out`, timeout, temporary network failure, HTTP 5xx), OR the case is `DIRTY_CHECKPOINT_RECOVERY` with `recovery_request_validated=true`.

For `DIRTY_CHECKPOINT_RECOVERY` ({MARKER}), the request itself never authorizes a retry. The execution-governance plugin revalidates the exact live workspace + HEAD + git-status SHA256 at decision time and fails closed if it changed. `RETRY_AUTHORIZED` is appropriate only when those deterministic facts are valid; a target in `triage` may be requeued only by this governed path.
'''
    return replace_once(text, anchor, replacement, "execution-governor dirty recovery decision rule")


def sha10(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch Hermes dirty checkpoint recovery request flow")
    parser.add_argument("--root", default="/opt/data")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve(strict=False)
    files = {
        "governance_guard": root / "plugins" / "governance-guard" / "__init__.py",
        "governance_guard_manifest": root / "plugins" / "governance-guard" / "plugin.yaml",
        "execution_governance": root / "plugins" / "execution-governance" / "__init__.py",
        "execution_governance_manifest": root / "plugins" / "execution-governance" / "plugin.yaml",
        "orchestrator_soul": root / "profiles" / "implementation-orchestrator" / "SOUL.md",
        "governor_soul": root / "profiles" / "execution-governor" / "SOUL.md",
    }

    originals = {name: read_text(path) for name, path in files.items()}
    patched = dict(originals)
    patched["governance_guard"] = patch_governance_guard(originals["governance_guard"])
    patched["execution_governance"] = patch_execution_governance(originals["execution_governance"])
    patched["orchestrator_soul"] = patch_orchestrator_soul(originals["orchestrator_soul"])
    patched["governor_soul"] = patch_governor_soul(originals["governor_soul"])
    patched["governance_guard_manifest"] = patch_version(originals["governance_guard_manifest"], "0.3.0", "0.3.1", "governance-guard manifest")
    patched["execution_governance_manifest"] = patch_version(originals["execution_governance_manifest"], "0.3.0", "0.3.1", "execution-governance manifest")

    changed = [name for name in files if originals[name] != patched[name]]
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
    backup = root / f".dirty-checkpoint-recovery-backup-{stamp}"
    for name in changed:
        src = files[name]
        dst = backup / src.relative_to(root)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)

    written: list[str] = []
    try:
        for name in changed:
            atomic_write(files[name], patched[name])
            written.append(name)
    except Exception:
        for name in written:
            src = backup / files[name].relative_to(root)
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
