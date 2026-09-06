#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import inspect
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CONTROLLER_VERSION = "1.1.0"
GATE_NAME = "GATE_E_CANONICAL_DETERMINISTIC"
POSITIVE_PATH = "docs/_smoke/gate-e-canonical-positive.md"
NEGATIVE_PATH = "docs/_smoke/gate-e-canonical-negative.md"
NEGATIVE_MUTATION_PATH = "docs/_smoke/gate-e-canonical-negative-mutation.md"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def jdump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def fail(msg: str) -> None:
    raise RuntimeError(msg)


def run(cmd: list[str], *, timeout: int = 30, cwd: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def git(repo: Path, *args: str, timeout: int = 60) -> str:
    p = run(["git", "-C", str(repo), *args], timeout=timeout)
    if p.returncode != 0:
        fail(f"git {' '.join(args)} failed: {(p.stderr or p.stdout).strip()[-1000:]}")
    return p.stdout


def git_snapshot(workspace: Path) -> dict[str, Any]:
    head = git(workspace, "rev-parse", "HEAD").strip()
    branch = git(workspace, "branch", "--show-current").strip()
    status = git(workspace, "status", "--porcelain=v1", "--untracked-files=all")
    return {
        "workspace": str(workspace.resolve()),
        "head": head,
        "branch": branch,
        "status": status,
        "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
        "changed_entries": len([x for x in status.splitlines() if x.strip()]),
    }


@contextlib.contextmanager
def temporary_env(**changes: str | None):
    old = {k: os.environ.get(k) for k in changes}
    try:
        for key, value in changes.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_module(name: str, path: Path):
    if not path.is_file():
        fail(f"required live plugin not found: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        fail(f"cannot load module spec: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def parse_json_response(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(str(raw))
    except Exception as exc:
        fail(f"expected JSON response, got: {str(raw)[:800]} ({exc})")
    if not isinstance(value, dict):
        fail(f"expected JSON object response, got: {type(value).__name__}")
    return value


class Controller:
    def __init__(self, root: Path, repo: Path, board: str, timeout: int):
        self.root = root.resolve()
        self.repo = repo.resolve()
        self.board = board
        self.timeout = timeout
        self.run_id = datetime.now().strftime("gatee_%Y%m%d_%H%M%S")
        self.log_dir = self.root / "logs" / "gate-e-canonical" / self.run_id
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.log_dir / "state.json"
        self.report_path = self.log_dir / "report.json"
        self.events_path = self.log_dir / "controller-events.jsonl"
        self.state: dict[str, Any] = {
            "gate": GATE_NAME,
            "controller_version": CONTROLLER_VERSION,
            "run_id": self.run_id,
            "started_at": now_iso(),
            "board": self.board,
            "repo": str(self.repo),
            "result": "RUNNING",
            "positive": {},
            "negative": {},
        }
        from hermes_cli import kanban_db as kb
        self.kb = kb
        self._persist()

    def _persist(self) -> None:
        tmp = self.state_path.with_suffix(".json.tmp")
        tmp.write_text(jdump(self.state) + "\n", encoding="utf-8")
        os.replace(tmp, self.state_path)

    def audit(self, event: str, **data: Any) -> None:
        rec = {"ts": now_iso(), "event": event, **data}
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
        print(f"[{event}] {json.dumps(data, ensure_ascii=False, sort_keys=True)}", flush=True)

    def conn(self):
        return self.kb.connect(board=self.board)

    def task(self, task_id: str):
        conn = self.conn()
        try:
            return self.kb.get_task(conn, task_id)
        finally:
            conn.close()

    def comments(self, task_id: str) -> list[Any]:
        conn = self.conn()
        try:
            return list(self.kb.list_comments(conn, task_id) or [])
        finally:
            conn.close()

    def has_comment(self, task_id: str, marker: str) -> bool:
        return any(marker in str(getattr(c, "body", "") or "") for c in self.comments(task_id))

    def add_comment(self, task_id: str, body: str) -> None:
        conn = self.conn()
        try:
            self.kb.add_comment(conn, task_id, author="gate-e-controller", body=body)
        finally:
            conn.close()

    def task_events(self, task_id: str) -> list[dict[str, Any]]:
        conn = self.conn()
        try:
            rows = conn.execute(
                "SELECT rowid AS rid, run_id, kind, payload, created_at "
                "FROM task_events WHERE task_id=? ORDER BY rowid",
                (task_id,),
            ).fetchall()
            out = []
            for row in rows:
                payload = row["payload"]
                try:
                    payload_obj = json.loads(payload) if payload else None
                except Exception:
                    payload_obj = payload
                out.append({
                    "rid": row["rid"], "run_id": row["run_id"], "kind": row["kind"],
                    "payload": payload_obj, "created_at": row["created_at"],
                })
            return out
        finally:
            conn.close()

    def wait(self, desc: str, predicate: Callable[[], Any], timeout: int | None = None) -> Any:
        deadline = time.monotonic() + (timeout or self.timeout)
        last_print = 0.0
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                self.audit("wait_satisfied", description=desc)
                return value
            now = time.monotonic()
            if now - last_print >= 15:
                self.audit("waiting", description=desc)
                last_print = now
            time.sleep(2)
        fail(f"timeout waiting for: {desc}")

    def create_worker(self, title: str, body: str) -> str:
        conn = self.conn()
        try:
            sig = inspect.signature(self.kb.create_task)
            candidates = {
                "title": title,
                "body": body,
                "assignee": "implementation-worker",
                "created_by": "gate-e-controller",
                "workspace_kind": "worktree",
                "initial_status": "blocked",
                "max_retries": 4,
                "board": self.board,
            }
            kwargs = {k: v for k, v in candidates.items() if k in sig.parameters}
            for required in ("title", "assignee", "workspace_kind", "initial_status"):
                if required not in kwargs:
                    fail(f"Hermes create_task lacks required parameter for deterministic Gate E: {required}")
            task_id = str(self.kb.create_task(conn, **kwargs))
            task = self.kb.get_task(conn, task_id)
            if task is None:
                fail("created worker task cannot be read back")
            if str(getattr(task, "status", "")) != "blocked":
                fail(f"new worker must start blocked, got {getattr(task, 'status', None)}")
            self.kb.add_comment(
                conn, task_id, author="gate-e-controller",
                body=(
                    f"GATE_E_CONTROLLER_CREATED\ncontroller_run_id: {self.run_id}\n"
                    "coordination: deterministic_external_controller\n"
                    "root_llm: not_used\npush_performed: false\n"
                ),
            )
            return task_id
        finally:
            conn.close()

    def unblock(self, task_id: str, reason: str) -> None:
        conn = self.conn()
        try:
            before = self.kb.get_task(conn, task_id)
            ok = self.kb.unblock_task(conn, task_id)
            after = self.kb.get_task(conn, task_id)
            if not ok:
                fail(f"Hermes refused unblock for {task_id}; status={getattr(before, 'status', None)}")
            self.kb.add_comment(
                conn, task_id, author="gate-e-controller",
                body=f"GATE_E_CONTROLLER_REACTIVATION\nreason: {reason}\nresult_status: {getattr(after, 'status', None)}\n",
            )
        finally:
            conn.close()

    def workspace_for(self, task_id: str) -> Path:
        task = self.task(task_id)
        raw = str(getattr(task, "workspace_path", "") or "").strip()
        if not raw:
            fail(f"task {task_id} has no workspace_path")
        p = Path(raw).resolve()
        if not p.is_dir():
            fail(f"task workspace does not exist: {p}")
        return p

    def governance_state(self) -> dict[str, Any]:
        path = self.root / "profiles" / "execution-governor" / "governance" / "state.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"cannot read governance state: {exc}")
        if not isinstance(value, dict):
            fail("governance state is not object")
        return value

    def case_file(self, case_id: str) -> dict[str, Any]:
        path = self.root / "profiles" / "execution-governor" / "governance" / "cases" / f"{case_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            fail(f"cannot read case {case_id}: {exc}")
        if not isinstance(value, dict):
            fail(f"case {case_id} is not object")
        return value

    def positive_body(self, base_ref: str) -> str:
        return f'''# GATE E CANONICAL — POSITIVE RECOVERY WORKER

controller_run_id: {self.run_id}
base_ref: {base_ref}
allowed_paths:
  - {POSITIVE_PATH}
workspace_kind: worktree
goal_mode: false

This card is controlled by an external deterministic Gate E controller. Do not create children. Never push, integrate, reset, clean, delete WIP, reprovision the worktree, start Godot/GUT/GWRM, or modify any file except `{POSITIVE_PATH}`.

On EVERY actual worker run, call `worktree_guardian_verify` before reading/editing technical files.

PHASE A — when `{POSITIVE_PATH}` does not exist:
1. Require worktree_guardian validation_passed=true, initial_git_clean=true, authorized_retry_checkpoint=false, resume_epoch=null, HEAD==base_ref.
2. Create exactly `{POSITIVE_PATH}` with:

# Gate E Canonical Positive Recovery
controller_run_id: {self.run_id}
phase: checkpoint-A
checkpoint_A_payload: ORIGINAL_WIP_A_MUST_SURVIVE

3. Do NOT git add or commit.
4. Compute exact `git status --porcelain=v1 --untracked-files=all` and SHA-256 of those bytes.
5. Comment `GATE_E_CHECKPOINT_A_CREATED` with workspace, branch, HEAD, status, status_sha256, and `push_performed: false`.
6. Call `kanban_block` with kind=`needs_input` and exact reason `GATE_E_CHECKPOINT_A_READY_FOR_BLIND_REDISPATCH`. Do not complete.

PHASE A RECOVERY — when the file exists with `phase: checkpoint-A`:
1. Require worktree_guardian validation_passed=true, authorized_retry_checkpoint=true and resume_epoch > 0. If not, FAIL CLOSED without touching the file.
2. Confirm `ORIGINAL_WIP_A_MUST_SURVIVE` is unchanged.
3. Append:

phase: checkpoint-A-recovered
recovery_A_authorized: true
resume_epoch_A: <real value>
checkpoint_A_preserved: true

4. Validate only the allowed path changed and `git diff --check` passes.
5. `git add -- {POSITIVE_PATH}` and commit exactly `test(smoke): gate E positive dirty recovery`.
6. Confirm worktree clean immediately after commit.
7. Comment `GATE_E_POSITIVE_RECOVERY_PASS` with commit, workspace, branch, resume_epoch_A, `checkpoint_A_preserved: true`, `worktree_guardian_authorized: true`, `final_worktree_clean: true`, `push_performed: false`.
8. Call `kanban_complete`. Do not create any later dirty state.
'''

    def negative_body(self, base_ref: str) -> str:
        return f'''# GATE E CANONICAL — NEGATIVE FINGERPRINT WORKER

controller_run_id: {self.run_id}
base_ref: {base_ref}
allowed_paths:
  - {NEGATIVE_PATH}
  - {NEGATIVE_MUTATION_PATH}
workspace_kind: worktree
goal_mode: false

This is a single-run setup worker for the deterministic negative boundary probe. Never push, integrate, reset, clean, delete WIP, reprovision the worktree, start Godot/GUT/GWRM, or modify any file except `{NEGATIVE_PATH}`. The second allowed path `{NEGATIVE_MUTATION_PATH}` is reserved exclusively for the deterministic controller fault injection; the worker must never create or edit it.

1. Call `worktree_guardian_verify`. Require validation_passed=true, initial_git_clean=true, authorized_retry_checkpoint=false, resume_epoch=null, HEAD==base_ref.
2. Create exactly `{NEGATIVE_PATH}` with:

# Gate E Canonical Negative Fingerprint
controller_run_id: {self.run_id}
phase: checkpoint-B
checkpoint_B_payload: STALE_FINGERPRINT_MUST_BE_REJECTED

3. Do NOT git add or commit.
4. Capture exact `git status --porcelain=v1 --untracked-files=all`, HEAD, and SHA-256 of status bytes.
5. Comment `GATE_E_CHECKPOINT_B_CREATED` with workspace, branch, HEAD, status, status_sha256, `push_performed: false`.
6. Call `kanban_block` with kind=`needs_input` and exact reason `GATE_E_CHECKPOINT_B_READY_FOR_DETERMINISTIC_NEGATIVE_PROBE`.
7. Do NOT complete and do NOT request recovery yourself. The deterministic controller owns the probe.
'''

    def run_positive(self, base_ref: str) -> dict[str, Any]:
        body = self.positive_body(base_ref)
        tid = self.create_worker(f"GATE E E1 — Positive dirty recovery [{self.run_id}]", body)
        self.state["positive"]["task_id"] = tid
        self._persist()
        self.audit("positive_created", task_id=tid)
        self.unblock(tid, "start fresh positive Gate E subtest")

        self.wait(
            "positive worker creates checkpoint A and blocks",
            lambda: (
                self.task(tid)
                if str(getattr(self.task(tid), "status", "")) in {"blocked", "triage"}
                and self.has_comment(tid, "GATE_E_CHECKPOINT_A_CREATED")
                else None
            ),
        )
        ws = self.workspace_for(tid)
        snap_a = git_snapshot(ws)
        file_a = ws / POSITIVE_PATH
        if not file_a.is_file() or "ORIGINAL_WIP_A_MUST_SURVIVE" not in file_a.read_text(encoding="utf-8"):
            fail("positive checkpoint A payload missing")
        if snap_a["changed_entries"] != 1 or POSITIVE_PATH not in snap_a["status"]:
            fail(f"positive checkpoint A unexpected git status: {snap_a['status']!r}")
        self.state["positive"]["checkpoint_A"] = snap_a
        self._persist()

        initial_spawns = [e for e in self.task_events(tid) if e["kind"] == "spawned"]
        if len(initial_spawns) != 1:
            fail(f"positive worker expected exactly one initial spawn, got {len(initial_spawns)}")
        self.unblock(tid, "blind dirty redispatch probe without authorization")

        # Wait for deterministic retry guard evidence, then for real governed recovery to finish.
        self.wait(
            "retry-checkpoint-guard refuses blind positive redispatch",
            lambda: self.has_comment(tid, "WORKTREE_RETRY_CHECKPOINT_GUARD"),
        )
        self.wait(
            "positive worker completes after governed recovery",
            lambda: self.task(tid) if str(getattr(self.task(tid), "status", "")) == "done" else None,
        )

        events = self.task_events(tid)
        kinds = [e["kind"] for e in events]
        guard_indexes = [i for i, e in enumerate(events) if e["kind"] == "respawn_guarded"]
        requeue_indexes = [i for i, e in enumerate(events) if e["kind"] == "governance_recovery_requeued"]
        spawn_indexes = [i for i, e in enumerate(events) if e["kind"] == "spawned"]
        if not guard_indexes:
            fail("positive subtest missing respawn_guarded")
        if not requeue_indexes:
            fail("positive subtest missing governance_recovery_requeued")
        guard_i = guard_indexes[0]
        requeue_i = next((i for i in requeue_indexes if i > guard_i), None)
        if requeue_i is None:
            fail("positive subtest has no governance requeue after guard")
        if any(guard_i < i < requeue_i for i in spawn_indexes):
            fail("worker spawned between blind guard and governance requeue")
        if not any(i > requeue_i for i in spawn_indexes):
            fail("no authorized worker spawn after governance requeue")

        if not self.has_comment(tid, "RETRY_AUTHORIZED"):
            fail("positive subtest missing Execution Governor RETRY_AUTHORIZED comment")
        if not self.has_comment(tid, "GATE_E_POSITIVE_RECOVERY_PASS"):
            fail("positive worker missing completion evidence comment")

        final_snap = git_snapshot(ws)
        if final_snap["status"].strip():
            fail(f"positive worktree must be clean after completion: {final_snap['status']!r}")
        text = file_a.read_text(encoding="utf-8")
        for token in ("ORIGINAL_WIP_A_MUST_SURVIVE", "recovery_A_authorized: true", "checkpoint_A_preserved: true"):
            if token not in text:
                fail(f"positive final file missing {token}")

        state = self.governance_state()
        task_state = (state.get("tasks") or {}).get(tid) or {}
        resume_epoch = int(task_state.get("resume_epoch") or 0)
        if resume_epoch <= 0:
            fail("positive resume_epoch was not incremented")

        result = {
            "result": "PASS",
            "task_id": tid,
            "workspace": str(ws),
            "checkpoint_A_status_sha256": snap_a["status_sha256"],
            "resume_epoch": resume_epoch,
            "final_head": final_snap["head"],
            "final_worktree_clean": True,
            "respawn_guarded": True,
            "governance_recovery_requeued": True,
            "push_performed": False,
        }
        self.state["positive"] = result
        self._persist()
        self.audit("positive_pass", **result)
        return result

    def run_negative(self, base_ref: str) -> dict[str, Any]:
        body = self.negative_body(base_ref)
        tid = self.create_worker(f"GATE E E2 — Stale fingerprint rejection [{self.run_id}]", body)
        self.state["negative"]["task_id"] = tid
        self._persist()
        self.audit("negative_created", task_id=tid)
        self.unblock(tid, "start fresh negative Gate E subtest")

        self.wait(
            "negative worker creates checkpoint B and blocks",
            lambda: (
                self.task(tid)
                if str(getattr(self.task(tid), "status", "")) in {"blocked", "triage"}
                and self.has_comment(tid, "GATE_E_CHECKPOINT_B_CREATED")
                else None
            ),
        )
        ws = self.workspace_for(tid)
        file_b = ws / NEGATIVE_PATH
        snap_b = git_snapshot(ws)
        if not file_b.is_file() or "STALE_FINGERPRINT_MUST_BE_REJECTED" not in file_b.read_text(encoding="utf-8"):
            fail("negative checkpoint B payload missing")
        if snap_b["changed_entries"] != 1 or NEGATIVE_PATH not in snap_b["status"]:
            fail(f"negative checkpoint B unexpected git status: {snap_b['status']!r}")
        if self.has_comment(tid, "WORKTREE_RETRY_CHECKPOINT_GUARD"):
            fail("negative worker unexpectedly passed through retry-checkpoint-guard before controlled probe")
        before_spawns = [e for e in self.task_events(tid) if e["kind"] == "spawned"]
        if len(before_spawns) != 1:
            fail(f"negative worker expected exactly one setup spawn, got {len(before_spawns)}")

        gg_path = self.root / "plugins" / "governance-guard" / "__init__.py"
        gg = load_module(f"gate_e_governance_guard_{self.run_id}", gg_path)
        if not hasattr(gg, "_ensure_dirty_recovery_case_for_task") or not hasattr(gg, "_ensure_board_governor"):
            fail("live governance-guard lacks v2.1 dirty recovery helpers")
        real_ensure = gg._ensure_board_governor
        held_calls: list[dict[str, Any]] = []

        def held_ensure(*args, **kwargs):
            held_calls.append({"args_len": len(args), "kwargs": sorted(kwargs.keys())})
            return {"ok": True, "method": "gate_e_controller_hold", "held": True}

        gg._ensure_board_governor = held_ensure
        try:
            raw = gg._ensure_dirty_recovery_case_for_task(
                tid,
                self.board,
                reason="Gate E deterministic negative fingerprint probe",
                source="gate_e_controller_negative_probe",
            )
        finally:
            gg._ensure_board_governor = real_ensure
        created = raw if isinstance(raw, dict) else parse_json_response(raw)
        if not created.get("ok") or not created.get("case_id"):
            fail(f"negative held case creation failed: {created}")
        case_id = str(created["case_id"])
        case = self.case_file(case_id)
        case_checkpoint = ((case.get("recovery_request") or {}).get("checkpoint") or {})
        if str(case_checkpoint.get("status_sha256") or "") != snap_b["status_sha256"]:
            fail("negative case did not capture checkpoint B fingerprint")
        if not held_calls:
            fail("negative probe did not hold Governor wake-up")

        # Ensure the held case is not in the active governor queue.
        state_before = self.governance_state()
        board_entry = ((state_before.get("governance_boards") or {}).get(self.board) or {})
        if case_id in list(board_entry.get("pending_cases") or []) or str(board_entry.get("active_case_id") or "") == case_id:
            fail("negative held case unexpectedly entered Governor queue")
        if ((state_before.get("tasks") or {}).get(tid) or {}).get("authorized_recovery_checkpoint"):
            fail("negative checkpoint B was authorized before mutation")

        # Controlled B -> C topology mutation, same workspace and same HEAD.
        # v2.1 defines checkpoint identity from exact git-status bytes. Mutating only
        # the contents of an already-dirty path would intentionally keep that
        # identity unchanged. Therefore the canonical negative probe adds a second
        # allowed untracked path so the production fingerprint must change.
        mutation_file = ws / NEGATIVE_MUTATION_PATH
        if mutation_file.exists():
            fail("negative mutation probe path unexpectedly exists before B -> C mutation")
        mutation_file.parent.mkdir(parents=True, exist_ok=True)
        mutation_file.write_text(
            "# Gate E Canonical Negative Mutation Probe\n"
            f"controller_run_id: {self.run_id}\n"
            "phase: checkpoint-C\n"
            "fingerprint_C_topology_mutation_probe: true\n",
            encoding="utf-8",
        )
        snap_c = git_snapshot(ws)
        if snap_c["workspace"] != snap_b["workspace"] or snap_c["head"] != snap_b["head"]:
            fail("negative mutation changed workspace or HEAD")
        if snap_c["status_sha256"] == snap_b["status_sha256"]:
            fail("negative topology mutation did not change status fingerprint")
        if snap_c["changed_entries"] != 2 or NEGATIVE_PATH not in snap_c["status"] or NEGATIVE_MUTATION_PATH not in snap_c["status"]:
            fail(f"negative checkpoint C unexpected git status: {snap_c['status']!r}")
        self.add_comment(
            tid,
            "GATE_E_FINGERPRINT_MUTATED_B_TO_C\n"
            f"case_id: {case_id}\n"
            f"status_sha256_B: {snap_b['status_sha256']}\n"
            f"status_sha256_C: {snap_c['status_sha256']}\n"
            "workspace_matches: true\nhead_matches: true\nfingerprint_changed: true\n",
        )

        # Call the exact production decision-application boundary directly.
        eg_path = self.root / "plugins" / "execution-governance" / "__init__.py"
        eg = load_module(f"gate_e_execution_governance_{self.run_id}", eg_path)
        governor_home = str(self.root / "profiles" / "execution-governor")
        with temporary_env(HERMES_HOME=governor_home, HERMES_KANBAN_BOARD=self.board):
            retry_raw = eg.governance_decide({
                "case_id": case_id,
                "decision": "RETRY_AUTHORIZED",
                "rationale": "gate_e_negative_probe_force_retry_decision",
            })
        retry_result = parse_json_response(retry_raw)
        detail = f"{retry_result.get('error', '')} {retry_result.get('detail', '')}".lower()
        if retry_result.get("ok"):
            fail(f"STALE FINGERPRINT SAFETY FAILURE: RETRY_AUTHORIZED unexpectedly succeeded: {retry_result}")
        if "fingerprint" not in detail or "changed" not in detail:
            fail(f"negative retry was rejected for the wrong reason: {retry_result}")

        state_after_reject = self.governance_state()
        if ((state_after_reject.get("tasks") or {}).get(tid) or {}).get("authorized_recovery_checkpoint"):
            fail("stale fingerprint rejection still created an authorization")
        task_after = self.task(tid)
        if str(getattr(task_after, "status", "")) not in {"blocked", "triage"}:
            fail(f"negative task was reactivated after stale rejection: {getattr(task_after, 'status', None)}")
        after_spawns = [e for e in self.task_events(tid) if e["kind"] == "spawned"]
        if len(after_spawns) != 1:
            fail("negative task spawned after stale fingerprint rejection")

        # Terminalize only this controller-held test case through the normal governance API.
        with temporary_env(HERMES_HOME=governor_home, HERMES_KANBAN_BOARD=self.board):
            terminal_raw = eg.governance_decide({
                "case_id": case_id,
                "decision": "HUMAN_REQUIRED",
                "rationale": "Gate E negative probe completed; preserve rejected stale checkpoint as evidence",
            })
        terminal = parse_json_response(terminal_raw)
        if not terminal.get("ok"):
            fail(f"could not terminalize controller-held negative case: {terminal}")

        self.add_comment(
            tid,
            "GATE_E_NEGATIVE_FINGERPRINT_PASS\n"
            f"case_id: {case_id}\n"
            f"fingerprint_B: {snap_b['status_sha256']}\n"
            f"fingerprint_C: {snap_c['status_sha256']}\n"
            "stale_case_rejected: true\n"
            "authorization_created_from_B: false\n"
            "worker_spawned_from_stale_case: false\n"
            "negative_evidence_preserved_dirty: true\nmutation_kind: added_second_untracked_path\npush_performed: false\n",
        )

        result = {
            "result": "PASS",
            "task_id": tid,
            "workspace": str(ws),
            "case_id": case_id,
            "fingerprint_B": snap_b["status_sha256"],
            "fingerprint_C": snap_c["status_sha256"],
            "fingerprint_changed": True,
            "mutation_kind": "added_second_untracked_path",
            "stale_case_rejected": True,
            "authorization_created_from_B": False,
            "worker_spawned_from_stale_case": False,
            "case_terminal_status": "HUMAN_REQUIRED",
            "evidence_worktree_intentionally_dirty": True,
            "push_performed": False,
        }
        self.state["negative"] = result
        self._persist()
        self.audit("negative_pass", **result)
        return result

    def run_all(self) -> dict[str, Any]:
        # Preflight live hardening and repository.
        if not (self.root / "plugins" / "governance-guard" / "__init__.py").is_file():
            fail("governance-guard live plugin missing")
        if not (self.root / "plugins" / "execution-governance" / "__init__.py").is_file():
            fail("execution-governance live plugin missing")
        gg_text = (self.root / "plugins" / "governance-guard" / "__init__.py").read_text(encoding="utf-8")
        eg_text = (self.root / "plugins" / "execution-governance" / "__init__.py").read_text(encoding="utf-8")
        for token in (
            "HERMES_DIRTY_CHECKPOINT_RECOVERY_V2_2026_09_04",
            "_ensure_dirty_recovery_case_for_task",
        ):
            if token not in gg_text:
                fail(f"governance-guard v2.1 preflight missing {token}")
        for token in (
            "HERMES_DIRTY_CHECKPOINT_RECOVERY_2026_09_04",
            "recovery checkpoint fingerprint changed after governance case creation",
        ):
            if token not in eg_text:
                fail(f"execution-governance v1 preflight missing {token}")
        inside = git(self.repo, "rev-parse", "--is-inside-work-tree").strip()
        if inside != "true":
            fail("canonical repository path is not a Git worktree")
        base_ref = git(self.repo, "rev-parse", "HEAD").strip()
        # Do not scan the repository-root working tree here. On large repositories,
        # `git status --untracked-files=all` can be expensive and is irrelevant to
        # linked worktrees created from the immutable base_ref. Freshness is instead
        # enforced inside each newly-provisioned worker worktree by worktree-guardian.
        tracked_smoke_paths = git(
            self.repo,
            "ls-tree",
            "-r",
            "--name-only",
            base_ref,
            "--",
            POSITIVE_PATH,
            NEGATIVE_PATH,
            NEGATIVE_MUTATION_PATH,
        )
        if tracked_smoke_paths.strip():
            fail(
                "canonical smoke paths already exist in base_ref; choose fresh paths before Gate E: "
                + tracked_smoke_paths.strip()
            )
        self.state["base_ref"] = base_ref
        self._persist()
        self.audit("preflight_pass", base_ref=base_ref, repo=str(self.repo), board=self.board)

        positive = self.run_positive(base_ref)
        negative = self.run_negative(base_ref)
        report = {
            "gate": GATE_NAME,
            "controller_version": CONTROLLER_VERSION,
            "run_id": self.run_id,
            "result": "PASS",
            "base_ref": base_ref,
            "positive": positive,
            "negative": negative,
            "safety": {
                "git_reset_used_by_controller": False,
                "git_clean_used_by_controller": False,
                "wip_deleted_by_controller": False,
                "replacement_worktree_created_by_controller": False,
                "push_performed": False,
                "integration_performed": False,
            },
            "completed_at": now_iso(),
        }
        self.report_path.write_text(jdump(report) + "\n", encoding="utf-8")
        self.state.update({"result": "PASS", "completed_at": report["completed_at"], "report": str(self.report_path)})
        self._persist()
        self.audit("gate_e_pass", report=str(self.report_path))
        print("\nGATE_E_CANONICAL: PASS")
        print(f"controller_run_id: {self.run_id}")
        print(f"positive_worker: {positive['task_id']}")
        print(f"negative_worker: {negative['task_id']}")
        print(f"report: {self.report_path}")
        return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Deterministic canonical Gate E controller")
    ap.add_argument("--root", default="/opt/data")
    ap.add_argument("--repo", default="/workspace/skill_system_framework")
    ap.add_argument("--board", default="default")
    ap.add_argument("--timeout-seconds", type=int, default=1800)
    args = ap.parse_args()

    ctl = Controller(Path(args.root), Path(args.repo), args.board, max(120, args.timeout_seconds))
    try:
        ctl.run_all()
        return 0
    except Exception as exc:
        ctl.state["result"] = "FAIL"
        ctl.state["failed_at"] = now_iso()
        ctl.state["error"] = f"{type(exc).__name__}: {exc}"
        ctl._persist()
        ctl.audit("gate_e_fail", error=ctl.state["error"])
        print("\nGATE_E_CANONICAL: FAIL", file=sys.stderr)
        print(ctl.state["error"], file=sys.stderr)
        print(f"state: {ctl.state_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
