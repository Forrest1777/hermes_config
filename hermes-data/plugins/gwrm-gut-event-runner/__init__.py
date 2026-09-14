"""Event-driven GUT execution for Hermes via GWRM.

Starts GUT once, parks the current Kanban run, and resumes only after the
terminal callback bridge unblocks the card. No test-status polling is used.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

TOOLSET = "gwrm_gut_event_runner"
STATE_DB = Path("/opt/data/logs/gwrm-gut-event-runner/state-v1.sqlite3")
AUDIT_PATH = Path("/opt/data/logs/gwrm-gut-event-runner/events.jsonl")
DEFAULT_CONTROL_URL = "http://host.docker.internal:8130"
HTTP_TIMEOUT_SECONDS = 10.0
ALLOWED_PROFILES = {"implementation-worker", "implementation-orchestrator"}

START_SCHEMA = {
    "name": "gwrm_gut_run_event_driven",
    "description": (
        "Start or reuse one GUT operation through GWRM. If non-terminal, atomically "
        "register the wait and park the current Kanban run. The run is resumed only "
        "by the authenticated terminal-event bridge; never poll GWRM status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "worktree_name": {"type": "string"},
            "test_directory": {"type": "string"},
            "test_script": {"type": "string"},
            "include_failure_output": {"type": "boolean"},
            "force_rerun": {"type": "boolean"},
        },
        "additionalProperties": False,
    },
}

COLLECT_SCHEMA = {
    "name": "gwrm_gut_collect_event_result",
    "description": (
        "Collect a terminal GUT result after a GWRM_GUT_TERMINAL_EVENT resumed the card. "
        "This reads durable event state only and never polls GWRM."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation_id": {"type": "string"},
            "include_failure_output": {"type": "boolean"},
        },
        "required": ["operation_id"],
        "additionalProperties": False,
    },
}


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False)


def _available() -> bool:
    return (
        os.environ.get("HERMES_PROFILE") in ALLOWED_PROFILES
        and bool(os.environ.get("HERMES_KANBAN_TASK"))
        and bool(os.environ.get("HERMES_KANBAN_RUN_ID"))
    )


def _audit(payload: dict[str, Any]) -> None:
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": int(time.time()),
            "profile": os.environ.get("HERMES_PROFILE", ""),
            "task_id": os.environ.get("HERMES_KANBAN_TASK", ""),
            "run_id": os.environ.get("HERMES_KANBAN_RUN_ID", ""),
            **payload,
        }
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _db() -> sqlite3.Connection:
    STATE_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(STATE_DB), timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=20000")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS waits (
            operation_id TEXT PRIMARY KEY,
            task_id TEXT NOT NULL,
            run_id INTEGER NOT NULL,
            profile TEXT NOT NULL,
            board TEXT,
            hermes_home TEXT,
            worktree_name TEXT NOT NULL,
            selection_type TEXT NOT NULL,
            selection_value TEXT NOT NULL,
            git_fingerprint TEXT,
            state TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL
        );
        CREATE INDEX IF NOT EXISTS waits_task_selection
            ON waits(task_id, selection_type, selection_value, updated_at DESC);

        CREATE TABLE IF NOT EXISTS terminal_events (
            operation_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            received_at INTEGER NOT NULL
        );
        """
    )
    return conn


def _server_env() -> dict[str, str]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        lsp_cfg = cfg.get("lsp") if isinstance(cfg, dict) else {}
        servers = lsp_cfg.get("servers") if isinstance(lsp_cfg, dict) else {}
        server = servers.get("godot-gdscript") if isinstance(servers, dict) else {}
        raw = server.get("env") if isinstance(server, dict) else {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _resolve_runtime() -> tuple[str, str]:
    env = _server_env()
    control_url = str(
        os.environ.get("GWRM_CONTROL_URL")
        or env.get("GWRM_CONTROL_URL")
        or DEFAULT_CONTROL_URL
    ).rstrip("/")
    api_key = str(os.environ.get("GWRM_API_KEY") or env.get("GWRM_API_KEY") or "")
    return control_url, api_key


def _derive_worktree(explicit: Any = None) -> str | None:
    value = str(explicit or "").strip()
    if value:
        return value
    value = str(os.environ.get("GWRM_WORKTREE_NAME") or "").strip()
    if value:
        return value
    workspace = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if workspace:
        return Path(workspace).name
    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return task_id or None


def _workspace_path(worktree_name: str) -> Path | None:
    workspace = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if workspace:
        path = Path(workspace)
        if path.name == worktree_name and path.exists():
            return path
    try:
        for candidate in Path("/workspace").glob(f"*/.worktrees/{worktree_name}"):
            if candidate.is_dir():
                return candidate
    except Exception:
        pass
    return None


def _git_state_fingerprint(worktree_name: str) -> str | None:
    path = _workspace_path(worktree_name)
    if path is None:
        return None
    try:
        def run_git(*args: str) -> bytes:
            proc = subprocess.run(
                ["git", "-C", str(path), *args],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
                timeout=15,
            )
            return proc.stdout

        digest = hashlib.sha256()
        digest.update(run_git("rev-parse", "HEAD"))
        digest.update(run_git("diff", "--binary", "HEAD", "--"))
        untracked = run_git("ls-files", "--others", "--exclude-standard", "-z")
        digest.update(untracked)
        for raw_name in (name for name in untracked.split(b"\0") if name):
            try:
                rel = raw_name.decode("utf-8", errors="surrogateescape")
                target = path / rel
                if target.is_file():
                    digest.update(raw_name)
                    digest.update(target.read_bytes())
            except Exception:
                continue
        return digest.hexdigest()
    except Exception:
        return None


def _post_tool_call(
    control_url: str,
    api_key: str,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    body = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{control_url}/api/v1/tools/call",
        data=body,
        headers={"content-type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise RuntimeError(f"GWRM HTTP {exc.code}: {detail or exc.reason}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"GWRM request failed: {type(exc).__name__}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("GWRM control API returned non-object response")
    if "error" in payload and "result" not in payload:
        raise RuntimeError(str(payload.get("error") or "GWRM control API error"))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("GWRM response missing object field 'result'")
    return result


def _selection(args: dict[str, Any]) -> tuple[str, str] | None:
    directory = str(args.get("test_directory") or "").strip()
    script = str(args.get("test_script") or "").strip()
    if bool(directory) == bool(script):
        return None
    return ("directory", directory) if directory else ("script", script)


def _compact_terminal(payload: dict[str, Any], include_failure_output: bool = True) -> dict[str, Any]:
    status = str(payload.get("status") or "")
    operation_id = str(payload.get("operation_id") or "")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    error = payload.get("error")

    passed = result.get("passed")
    counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
    has_structured_failure = bool(
        result.get("junit_xml_generated")
        or result.get("failure")
        or result.get("fatal_patterns")
        or counts.get("tests") is not None
        or counts.get("asserts") is not None
    )

    if status == "failed" and not result:
        reason = "GWRM_OPERATION_FAILED"
        ok = False
        final_passed = False
    elif passed is True:
        reason = "TESTS_PASSED"
        ok = True
        final_passed = True
    elif passed is False and has_structured_failure:
        reason = "TESTS_FAILED"
        ok = True
        final_passed = False
    elif passed is False:
        reason = "GUT_RESULT_UNVERIFIED"
        ok = False
        final_passed = False
    else:
        reason = "GUT_RESULT_UNVERIFIED"
        ok = False
        final_passed = False

    compact: dict[str, Any] = {
        "ok": ok,
        "reason": reason,
        "terminal": True,
        "passed": final_passed,
        "operation_id": operation_id,
        "status": status,
        "counts": counts,
        "exit_code": result.get("exit_code"),
        "timed_out": result.get("timed_out"),
        "duration_ms": result.get("duration_ms"),
        "result_source": result.get("result_source"),
        "junit_xml_generated": result.get("junit_xml_generated"),
    }
    if error:
        compact["error"] = str(error)[:2000]
    if include_failure_output and not final_passed:
        stdout = str(result.get("stdout") or "").strip()
        stderr = str(result.get("stderr") or "").strip()
        if stdout:
            compact["stdout_tail"] = stdout[-3000:]
        if stderr:
            compact["stderr_tail"] = stderr[-3000:]
    return compact


# HERMES_GWRM_RESULT_CURRENT_RUN_BINDING_2026_09_14
def _governor_state_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        home = Path(
            get_hermes_home()
        ).resolve(strict=False)
    except Exception:
        home = Path(
            os.environ.get("HERMES_HOME", "/opt/data")
        ).resolve(strict=False)

    if home.name == "execution-governor":
        governor = home
    elif home.parent.name == "profiles":
        governor = home.parent / "execution-governor"
    else:
        governor = (
            home / "profiles" / "execution-governor"
        )

    return governor / "governance" / "state.json"


def _expected_resume_operation_for_current_run() -> str | None:
    task_id = str(
        os.environ.get("HERMES_KANBAN_TASK") or ""
    ).strip()
    run_id = str(
        os.environ.get("HERMES_KANBAN_RUN_ID") or ""
    ).strip()
    if not task_id or not run_id:
        return None

    try:
        state = json.loads(
            _governor_state_path().read_text(
                encoding="utf-8"
            )
        )
        task = (
            (state.get("tasks") or {}).get(task_id)
            or {}
        )
        run = (
            (task.get("runs") or {}).get(run_id)
            or {}
        )
        operation_id = str(
            run.get("gwrm_event_resume_operation_id")
            or ""
        ).strip()
        return (
            operation_id
            if operation_id.startswith("gut_")
            else None
        )
    except Exception:
        return None


def _terminal_cacheable(
    payload: dict[str, Any],
) -> bool:
    compact = _compact_terminal(
        payload,
        include_failure_output=False,
    )
    return compact.get("reason") in {
        "TESTS_PASSED",
        "TESTS_FAILED",
    }



def _get_terminal_event(operation_id: str) -> dict[str, Any] | None:
    conn = _db()
    try:
        row = conn.execute(
            "SELECT payload_json FROM terminal_events WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        return payload if isinstance(payload, dict) else None
    finally:
        conn.close()


def _find_cached_terminal(
    task_id: str,
    selection_type: str,
    selection_value: str,
    fingerprint: str | None,
) -> tuple[str, dict[str, Any]] | None:
    conn = _db()
    try:
        rows = conn.execute(
            """
            SELECT operation_id, git_fingerprint
            FROM waits
            WHERE task_id = ? AND selection_type = ? AND selection_value = ?
            ORDER BY updated_at DESC
            LIMIT 10
            """,
            (task_id, selection_type, selection_value),
        ).fetchall()
        for row in rows:
            if fingerprint and row["git_fingerprint"] and row["git_fingerprint"] != fingerprint:
                continue
            event = conn.execute(
                "SELECT payload_json FROM terminal_events WHERE operation_id = ?",
                (row["operation_id"],),
            ).fetchone()
            if not event:
                continue
            payload = json.loads(event["payload_json"])
            if (
                isinstance(payload, dict)
                and _terminal_cacheable(payload)
            ):
                return str(row["operation_id"]), payload
        return None
    finally:
        conn.close()


def _register_wait(
    operation_id: str,
    task_id: str,
    run_id: int,
    profile: str,
    board: str | None,
    hermes_home: str,
    worktree_name: str,
    selection_type: str,
    selection_value: str,
    fingerprint: str | None,
) -> None:
    now = int(time.time())
    conn = _db()
    try:
        with conn:
            conn.execute(
                """
                INSERT INTO waits (
                    operation_id, task_id, run_id, profile, board, hermes_home,
                    worktree_name, selection_type, selection_value, git_fingerprint,
                    state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    run_id=excluded.run_id,
                    profile=excluded.profile,
                    board=excluded.board,
                    hermes_home=excluded.hermes_home,
                    worktree_name=excluded.worktree_name,
                    selection_type=excluded.selection_type,
                    selection_value=excluded.selection_value,
                    git_fingerprint=excluded.git_fingerprint,
                    updated_at=excluded.updated_at
                """,
                (
                    operation_id, task_id, run_id, profile, board, hermes_home,
                    worktree_name, selection_type, selection_value, fingerprint,
                    now, now,
                ),
            )
    finally:
        conn.close()


def _set_wait_state(operation_id: str, state: str) -> None:
    conn = _db()
    try:
        with conn:
            conn.execute(
                "UPDATE waits SET state = ?, updated_at = ? WHERE operation_id = ?",
                (state, int(time.time()), operation_id),
            )
    finally:
        conn.close()


def _park_current_task(operation_id: str, run_id: int) -> tuple[bool, str]:
    from hermes_cli import kanban_db as kb

    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    board = os.environ.get("HERMES_KANBAN_BOARD") or None
    conn = kb.connect(board=board)
    try:
        changed = kb.block_task(
            conn,
            task_id,
            reason=f"GWRM_EVENT_WAIT operation_id={operation_id}",
            kind=None,
            expected_run_id=run_id,
        )
        return bool(changed), task_id
    finally:
        conn.close()


def _start_handler(args: dict[str, Any], **kwargs) -> str:
    del kwargs
    if not _available():
        return _err("gwrm-gut-event-runner unavailable outside dispatched worker/orchestrator run")

    selection = _selection(args)
    if selection is None:
        return _err("provide exactly one of test_directory or test_script")
    selection_type, selection_value = selection

    worktree = _derive_worktree(args.get("worktree_name"))
    if not worktree:
        return _err("worktree_name could not be derived")

    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    profile = str(os.environ.get("HERMES_PROFILE") or "")
    board = os.environ.get("HERMES_KANBAN_BOARD") or None
    hermes_home = str(os.environ.get("HERMES_HOME") or "")
    try:
        run_id = int(str(os.environ.get("HERMES_KANBAN_RUN_ID") or ""))
    except ValueError:
        return _err("invalid HERMES_KANBAN_RUN_ID")

    fingerprint = _git_state_fingerprint(worktree)
    force_rerun = bool(args.get("force_rerun", False))
    include_failure_output = bool(args.get("include_failure_output", True))

    if not force_rerun:
        cached = _find_cached_terminal(
            task_id, selection_type, selection_value, fingerprint
        )
        if cached:
            operation_id, payload = cached
            compact = _compact_terminal(payload, include_failure_output)
            compact["cached"] = True
            compact["rerun_started"] = False
            return json.dumps(compact, ensure_ascii=False)

    control_url, api_key = _resolve_runtime()
    if not api_key:
        return _err("GWRM_API_KEY unavailable")

    tool_name = "run_gut_tests" if selection_type == "directory" else "run_gut_test_script"
    tool_args = {"worktree_name": worktree}
    tool_args["test_directory" if selection_type == "directory" else "test_script"] = selection_value

    try:
        operation = _post_tool_call(control_url, api_key, tool_name, tool_args)
    except Exception as exc:
        _audit({"event": "start_error", "error": f"{type(exc).__name__}: {exc}"[:1000]})
        return _err(f"{type(exc).__name__}: {exc}"[:1000], reason="GWRM_UNAVAILABLE")

    operation_id = str(operation.get("operation_id") or "").strip()
    if not operation_id:
        return _err("GWRM start returned no operation_id")

    if bool(operation.get("terminal")):
        payload = {
            "operation_id": operation_id,
            "worktree_name": worktree,
            "selection": operation.get("selection"),
            "status": operation.get("status"),
            "terminal": True,
            "result": operation.get("result"),
            "error": operation.get("error"),
        }
        return json.dumps(_compact_terminal(payload, include_failure_output), ensure_ascii=False)

    _register_wait(
        operation_id, task_id, run_id, profile, board, hermes_home,
        worktree, selection_type, selection_value, fingerprint,
    )

    # Race-safe: callback may have arrived after GWRM start but before wait registration.
    event = _get_terminal_event(operation_id)
    if event is not None:
        _set_wait_state(operation_id, "terminal_before_park")
        return json.dumps(_compact_terminal(event, include_failure_output), ensure_ascii=False)

    parked, _ = _park_current_task(operation_id, run_id)
    if not parked:
        _set_wait_state(operation_id, "park_failed")
        return _err(
            "Kanban park transition refused; GUT remains owned by GWRM",
            reason="PARK_FAILED",
            operation_id=operation_id,
            terminal=False,
        )

    _set_wait_state(operation_id, "parked")
    _audit({
        "event": "parked",
        "operation_id": operation_id,
        "worktree_name": worktree,
        "selection_type": selection_type,
        "selection_value": selection_value,
    })
    return _ok(
        reason="PARKED_EVENT_WAIT",
        terminal=False,
        operation_id=operation_id,
        worktree_name=worktree,
        rerun_started=not bool(operation.get("reused_existing_operation")),
        stop_current_run=True,
        instruction=(
            "Current Kanban run is parked. Stop immediately: do not poll, sleep, "
            "complete, commit, or call another tool. The terminal callback will resume the card."
        ),
    )


def _collect_handler(args: dict[str, Any], **kwargs) -> str:
    del kwargs
    if not _available():
        return _err("gwrm-gut-event-runner unavailable outside dispatched worker/orchestrator run")

    operation_id = str(args.get("operation_id") or "").strip()
    if not operation_id.startswith("gut_"):
        return _err("invalid operation_id")

    expected_operation_id = (
        _expected_resume_operation_for_current_run()
    )
    if expected_operation_id is None:
        _audit({
            "event": "collect_refused_unbound_run",
            "operation_id": operation_id,
        })
        return _err(
            "no GWRM operational resume is bound to the current Kanban run",
            reason="OPERATION_NOT_BOUND_TO_CURRENT_RUN",
            operation_id=operation_id,
        )

    if operation_id != expected_operation_id:
        _audit({
            "event": "collect_refused_stale_operation",
            "operation_id": operation_id,
            "expected_operation_id": expected_operation_id,
        })
        return _err(
            "operation_id does not belong to the current operational-resume run",
            reason="STALE_OPERATION_FOR_CURRENT_RUN",
            operation_id=operation_id,
            expected_operation_id=expected_operation_id,
        )

    event = _get_terminal_event(operation_id)
    if event is None:
        return _err(
            "terminal event not available; card must not actively wait",
            reason="EVENT_NOT_READY",
            terminal=False,
            operation_id=operation_id,
        )

    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")
    conn = _db()
    try:
        row = conn.execute(
            "SELECT task_id, state FROM waits WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if not row:
            return _err("operation_id is not registered to a Hermes wait")
        if str(row["task_id"]) != task_id:
            return _err("operation_id belongs to a different task")
        with conn:
            conn.execute(
                "UPDATE waits SET state='collected', updated_at=? WHERE operation_id=?",
                (int(time.time()), operation_id),
            )
    finally:
        conn.close()

    compact = _compact_terminal(
        event,
        bool(args.get("include_failure_output", True)),
    )
    compact["event_driven"] = True
    _audit({"event": "collected", "operation_id": operation_id, "reason": compact["reason"]})
    return json.dumps(compact, ensure_ascii=False)


def register(ctx) -> None:
    ctx.register_tool(
        name="gwrm_gut_run_event_driven",
        toolset=TOOLSET,
        schema=START_SCHEMA,
        handler=_start_handler,
        check_fn=_available,
        emoji="🧪",
    )
    ctx.register_tool(
        name="gwrm_gut_collect_event_result",
        toolset=TOOLSET,
        schema=COLLECT_SCHEMA,
        handler=_collect_handler,
        check_fn=_available,
        emoji="📬",
    )
