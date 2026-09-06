"""Hermes plugin: token-efficient long-running GUT execution via GWRM.

V0.1.1 adds a safe external-tool wait window, explicit continuation of an
existing operation, terminal-failure deduplication by Git state, and waiter
cleanup so long GUT runs do not trigger overlapping LLM/tool retries.

No Kanban mutation, Git mutation, GWRM lifecycle teardown, or push is performed.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("hermes_plugins.gwrm_gut_runner")
TOOLSET = "gwrm_gut_runner"

DEFAULT_CONTROL_URL = "http://host.docker.internal:8130"
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
DEFAULT_MAX_WAIT_SECONDS = 360
DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS = 420
DEFAULT_TOOL_TIMEOUT_SAFETY_MARGIN_SECONDS = 60
DEFAULT_HTTP_TIMEOUT_SECONDS = 10.0
DEFAULT_MAX_FAILURE_OUTPUT_CHARS = 3000
DEFAULT_MAX_CONSECUTIVE_STATUS_ERRORS = 3

CACHE_PATH = Path("/opt/data/logs/gwrm-gut-runner/terminal-cache-v2.json")
AUDIT_PATH = Path("/opt/data/logs/gwrm-gut-runner/events.jsonl")

# Process-local protection against overlapping waiters. The safe wait window is
# deliberately shorter than the observed Hermes tool timeout, so this set should
# normally be released before the outer tool call can expire.
_ACTIVE_WAITERS: set[str] = set()

RUN_SCHEMA = {
    "name": "gwrm_gut_run_and_wait",
    "description": (
        "Starts or reuses one supervised GUT operation through GWRM and waits internally "
        "for a safe bounded window. If the operation is still running, returns a non-terminal "
        "continuation result with operation_id; continue with gwrm_gut_wait_existing instead "
        "of starting/rerunning the same selection. Terminal failed results are deduplicated "
        "for the same Git state unless force_rerun=true."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "worktree_name": {
                "type": "string",
                "description": (
                    "GWRM worktree name. Optional in a dispatched Kanban worker when it "
                    "can be derived from HERMES_KANBAN_WORKSPACE/task context."
                ),
            },
            "test_directory": {
                "type": "string",
                "description": "GUT directory as res:// path. Mutually exclusive with test_script.",
            },
            "test_script": {
                "type": "string",
                "description": "Single GUT script as res:// path. Mutually exclusive with test_directory.",
            },
            "max_wait_seconds": {
                "type": "integer",
                "minimum": 30,
                "maximum": 1800,
                "description": (
                    "Requested wait window. For compatibility values above the safe external-tool "
                    "window are accepted but clamped internally."
                ),
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 30,
                "description": "Internal polling interval. Polls do not invoke the LLM.",
            },
            "include_failure_output": {
                "type": "boolean",
                "description": "Include compact stdout/stderr tails on test failure. Default true.",
            },
            "force_rerun": {
                "type": "boolean",
                "description": (
                    "Bypass the same-Git-state terminal-failure cache and intentionally start/reuse "
                    "the selection again. Default false; use only with explicit new evidence/reason."
                ),
            },
        },
        "additionalProperties": False,
    },
}

WAIT_SCHEMA = {
    "name": "gwrm_gut_wait_existing",
    "description": (
        "Waits for an already-known GWRM GUT operation_id. This tool never starts a GUT run. "
        "Use it after gwrm_gut_run_and_wait returns terminal=false / WAIT_WINDOW_EXPIRED."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation_id": {
                "type": "string",
                "description": "Existing GWRM GUT operation id, e.g. gut_<uuid>.",
            },
            "worktree_name": {
                "type": "string",
                "description": "Optional worktree name for audit/fingerprint context.",
            },
            "max_wait_seconds": {
                "type": "integer",
                "minimum": 30,
                "maximum": 1800,
                "description": "Requested wait window; clamped to the safe external-tool window.",
            },
            "poll_interval_seconds": {
                "type": "number",
                "minimum": 1,
                "maximum": 30,
            },
            "include_failure_output": {
                "type": "boolean",
            },
        },
        "required": ["operation_id"],
        "additionalProperties": False,
    },
}


class GwrmCallError(RuntimeError):
    """Control-API request to GWRM failed."""


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False)


def _server_env() -> dict[str, str]:
    """Reuse the same GWRM configuration source already used by godot-lsp."""
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


def _plugin_cfg() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "poll_interval_seconds": DEFAULT_POLL_INTERVAL_SECONDS,
        "max_wait_seconds": DEFAULT_MAX_WAIT_SECONDS,
        "external_tool_timeout_seconds": DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
        "tool_timeout_safety_margin_seconds": DEFAULT_TOOL_TIMEOUT_SAFETY_MARGIN_SECONDS,
        "http_timeout_seconds": DEFAULT_HTTP_TIMEOUT_SECONDS,
        "max_failure_output_chars": DEFAULT_MAX_FAILURE_OUTPUT_CHARS,
        "max_consecutive_status_errors": DEFAULT_MAX_CONSECUTIVE_STATUS_ERRORS,
    }
    try:
        from hermes_cli.config import load_config

        raw = load_config().get("gwrm_gut_runner", {}) or {}
        if isinstance(raw, dict):
            for key in tuple(defaults):
                if key in raw:
                    defaults[key] = raw[key]
    except Exception:
        pass
    return defaults


def _setting(env: dict[str, str], name: str, default: str = "") -> str:
    return str(os.environ.get(name) or env.get(name) or default)


def _resolve_runtime() -> tuple[str, str]:
    env = _server_env()
    control_url = _setting(env, "GWRM_CONTROL_URL", DEFAULT_CONTROL_URL).rstrip("/")
    api_key = _setting(env, "GWRM_API_KEY")
    return control_url, api_key


def _derive_worktree(explicit: Any = None) -> str | None:
    value = str(explicit or "").strip()
    if value:
        return value

    env_value = str(os.environ.get("GWRM_WORKTREE_NAME") or "").strip()
    if env_value:
        return env_value

    workspace = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    if workspace:
        path = Path(workspace)
        if path.parent.name == ".worktrees" and path.name:
            return path.name

    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    return task_id or None


def _audit(payload: dict[str, Any]) -> None:
    """Best-effort compact audit. Never records API keys or full test output."""
    try:
        AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": int(time.time()),
            "profile": str(os.environ.get("HERMES_PROFILE") or ""),
            "task_id": str(os.environ.get("HERMES_KANBAN_TASK") or ""),
            **payload,
        }
        with AUDIT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _trim_tail(value: Any, limit: int) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    limit = max(256, int(limit))
    if len(text) <= limit:
        return text
    return f"[tail truncated to {limit} chars]\n{text[-limit:]}"


def _bounded_number(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = float(default)
    return min(maximum, max(minimum, number))


def _wait_settings(args: dict[str, Any], cfg: dict[str, Any]) -> tuple[int, int, float, float, int, int]:
    requested = int(_bounded_number(
        args.get("max_wait_seconds"),
        cfg.get("max_wait_seconds", DEFAULT_MAX_WAIT_SECONDS),
        30,
        1800,
    ))
    external_timeout = _bounded_number(
        cfg.get("external_tool_timeout_seconds", DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS),
        DEFAULT_EXTERNAL_TOOL_TIMEOUT_SECONDS,
        60,
        3600,
    )
    margin = _bounded_number(
        cfg.get("tool_timeout_safety_margin_seconds", DEFAULT_TOOL_TIMEOUT_SAFETY_MARGIN_SECONDS),
        DEFAULT_TOOL_TIMEOUT_SAFETY_MARGIN_SECONDS,
        15,
        max(15, external_timeout - 30),
    )
    safe_cap = max(30, int(external_timeout - margin))
    effective = min(requested, safe_cap)
    poll_interval = _bounded_number(
        args.get("poll_interval_seconds"),
        cfg.get("poll_interval_seconds", DEFAULT_POLL_INTERVAL_SECONDS),
        1,
        30,
    )
    http_timeout = _bounded_number(
        cfg.get("http_timeout_seconds", DEFAULT_HTTP_TIMEOUT_SECONDS),
        DEFAULT_HTTP_TIMEOUT_SECONDS,
        1,
        min(60, max(1, effective / 4)),
    )
    failure_output_limit = int(_bounded_number(
        cfg.get("max_failure_output_chars", DEFAULT_MAX_FAILURE_OUTPUT_CHARS),
        DEFAULT_MAX_FAILURE_OUTPUT_CHARS,
        256,
        12000,
    ))
    max_status_errors = int(_bounded_number(
        cfg.get("max_consecutive_status_errors", DEFAULT_MAX_CONSECUTIVE_STATUS_ERRORS),
        DEFAULT_MAX_CONSECUTIVE_STATUS_ERRORS,
        1,
        10,
    ))
    return requested, effective, poll_interval, http_timeout, failure_output_limit, max_status_errors


def _post_tool_call_sync(
    control_url: str,
    api_key: str,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    body = json.dumps({"name": name, "arguments": arguments}, ensure_ascii=False).encode("utf-8")
    request = Request(
        f"{control_url}/api/v1/tools/call",
        data=body,
        headers={"content-type": "application/json", "X-API-Key": api_key},
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:1000]
        except Exception:
            pass
        raise GwrmCallError(f"GWRM HTTP {exc.code}: {detail or exc.reason}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise GwrmCallError(f"GWRM request failed: {type(exc).__name__}: {exc}") from exc

    if not isinstance(payload, dict):
        raise GwrmCallError("GWRM control API returned a non-object response")
    if "error" in payload and "result" not in payload:
        raise GwrmCallError(str(payload.get("error") or "GWRM control API error"))
    result = payload.get("result")
    if not isinstance(result, dict):
        raise GwrmCallError("GWRM control API response is missing object field 'result'")
    return result


async def _post_tool_call(
    control_url: str,
    api_key: str,
    name: str,
    arguments: dict[str, Any],
    timeout_seconds: float,
) -> dict[str, Any]:
    return await asyncio.to_thread(
        _post_tool_call_sync,
        control_url,
        api_key,
        name,
        arguments,
        timeout_seconds,
    )


def _get_health_sync(control_url: str, timeout_seconds: float) -> dict[str, Any]:
    request = Request(f"{control_url}/health", method="GET")
    try:
        with urlopen(request, timeout=max(1.0, float(timeout_seconds))) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception as exc:
        return {"ready": False, "error": f"{type(exc).__name__}: {exc}"}


async def _get_health(control_url: str, timeout_seconds: float) -> dict[str, Any]:
    return await asyncio.to_thread(_get_health_sync, control_url, timeout_seconds)


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
    """Hash HEAD + tracked diff + untracked names/contents for rerun dedupe."""
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


def _selection_key(test_directory: str, test_script: str) -> str:
    return f"script:{test_script}" if test_script else f"directory:{test_directory}"


def _cache_key(worktree_name: str, selection: str, fingerprint: str) -> str:
    raw = f"{worktree_name}\0{selection}\0{fingerprint}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _load_cache() -> dict[str, Any]:
    try:
        if not CACHE_PATH.exists():
            return {}
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _save_cache(cache: dict[str, Any]) -> None:
    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except Exception:
        pass


def _cached_terminal_failure(worktree_name: str, selection: str, fingerprint: str | None) -> dict[str, Any] | None:
    if not fingerprint:
        return None
    entry = _load_cache().get(_cache_key(worktree_name, selection, fingerprint))
    if not isinstance(entry, dict):
        return None
    payload = entry.get("payload")
    if not isinstance(payload, dict) or payload.get("passed") is not False or payload.get("terminal") is not True:
        return None
    return entry


def _remember_terminal_failure(
    worktree_name: str,
    selection: str,
    fingerprint: str | None,
    payload: dict[str, Any],
) -> None:
    if not fingerprint or payload.get("passed") is not False or payload.get("terminal") is not True:
        return
    cache = _load_cache()
    cache[_cache_key(worktree_name, selection, fingerprint)] = {
        "cached_at": int(time.time()),
        "worktree_name": worktree_name,
        "selection": selection,
        "fingerprint": fingerprint,
        "payload": payload,
    }
    # Keep the cache compact.
    if len(cache) > 128:
        ordered = sorted(
            cache.items(),
            key=lambda item: int(item[1].get("cached_at", 0)) if isinstance(item[1], dict) else 0,
            reverse=True,
        )
        cache = dict(ordered[:128])
    _save_cache(cache)



def _has_trustworthy_test_result(result: dict[str, Any]) -> bool:
    """Return True only when a red terminal result has actual test evidence.

    A completed process with exit_code=1, no JUnit, null counts and no parsed
    failure is not enough to call the tests failed. That shape can be produced
    by GUT/Godot execution problems and must not poison the rerun cache.
    """
    if bool(result.get("junit_xml_generated")) or str(result.get("result_source") or "") == "junit_xml":
        return True

    counts = result.get("counts")
    if isinstance(counts, dict):
        tests = counts.get("tests")
        failing = counts.get("failing_tests")
        errors = counts.get("errors")
        # A parser that actually counted tests/failures/errors is useful evidence.
        if tests is not None:
            return True
        try:
            if failing is not None and int(failing) > 0:
                return True
        except (TypeError, ValueError):
            pass
        try:
            if errors is not None and int(errors) > 0:
                return True
        except (TypeError, ValueError):
            pass

    failure = result.get("failure")
    if isinstance(failure, (list, tuple, dict, str)) and bool(failure):
        return True

    fatal_patterns = result.get("fatal_patterns")
    if isinstance(fatal_patterns, (list, tuple)) and len(fatal_patterns) > 0:
        return True

    return False

def _terminal_payload(
    operation: dict[str, Any],
    *,
    poll_count: int,
    wait_duration_ms: int,
    include_failure_output: bool,
    failure_output_limit: int,
    reused_existing_operation: bool,
) -> str:
    status = str(operation.get("status") or "unknown")
    operation_id = str(operation.get("operation_id") or "")
    result = operation.get("result")

    if status == "not_found":
        return _err(
            "GWRM operation not found",
            reason="OPERATION_NOT_FOUND",
            status=status,
            terminal=True,
            operation_id=operation_id,
            poll_count=poll_count,
            wait_duration_ms=wait_duration_ms,
        )

    if status == "failed" or (operation.get("terminal") and not isinstance(result, dict)):
        return _err(
            str(operation.get("error") or "GWRM GUT operation failed before producing a result"),
            reason="GWRM_OPERATION_FAILED",
            status=status,
            terminal=True,
            operation_id=operation_id,
            worktree_name=operation.get("worktree_name"),
            selection=operation.get("selection"),
            poll_count=poll_count,
            wait_duration_ms=wait_duration_ms,
            reused_existing_operation=reused_existing_operation,
        )

    if not isinstance(result, dict):
        return _err(
            "GWRM reported terminal operation without a structured result",
            reason="MALFORMED_TERMINAL_RESULT",
            status=status,
            terminal=bool(operation.get("terminal")),
            operation_id=operation_id,
            poll_count=poll_count,
            wait_duration_ms=wait_duration_ms,
        )

    passed = bool(result.get("passed"))
    trustworthy_test_result = passed or _has_trustworthy_test_result(result)

    if not passed and not trustworthy_test_result:
        payload: dict[str, Any] = {
            "ok": False,
            "error": (
                "GWRM reported a terminal non-passing process, but no trustworthy structured "
                "test result was produced (for example: missing JUnit with null test counts)."
            ),
            "reason": "GUT_RESULT_UNVERIFIED",
            "status": status,
            "terminal": True,
            "operation_id": operation_id,
            "passed": False,
            "counts": result.get("counts"),
            "failure": result.get("failure"),
            "fatal_patterns": result.get("fatal_patterns"),
            "exit_code": result.get("exit_code"),
            "signal": result.get("signal"),
            "timed_out": bool(result.get("timed_out")),
            "duration_ms": result.get("duration_ms"),
            "worktree_name": result.get("worktree_name") or operation.get("worktree_name"),
            "selection": result.get("selection") or operation.get("selection"),
            "junit_xml_generated": bool(result.get("junit_xml_generated")),
            "junit_xml_error": result.get("junit_xml_error"),
            "result_source": result.get("result_source"),
            "parser_warning": result.get("parser_warning"),
            "poll_count": poll_count,
            "wait_duration_ms": wait_duration_ms,
            "reused_existing_operation": reused_existing_operation,
            "rerun_started": False,
            "safe_recovery": (
                "Treat this as an execution/result-production problem, not a proven test assertion "
                "failure. Inspect GWRM/GUT evidence before rerunning."
            ),
            "raw_result_available_via": "get_gut_run_status",
        }
        if include_failure_output:
            stdout_tail = _trim_tail(result.get("stdout"), failure_output_limit)
            stderr_tail = _trim_tail(result.get("stderr"), failure_output_limit)
            if stdout_tail:
                payload["stdout_tail"] = stdout_tail
            if stderr_tail:
                payload["stderr_tail"] = stderr_tail
        return json.dumps(payload, ensure_ascii=False)

    payload: dict[str, Any] = {
        "ok": True,
        "reason": "TESTS_PASSED" if passed else "TESTS_FAILED",
        "status": status,
        "terminal": True,
        "operation_id": operation_id,
        "passed": passed,
        "counts": result.get("counts"),
        "failure": result.get("failure"),
        "fatal_patterns": result.get("fatal_patterns"),
        "exit_code": result.get("exit_code"),
        "signal": result.get("signal"),
        "timed_out": bool(result.get("timed_out")),
        "duration_ms": result.get("duration_ms"),
        "worktree_name": result.get("worktree_name") or operation.get("worktree_name"),
        "selection": result.get("selection") or operation.get("selection"),
        "junit_xml_generated": bool(result.get("junit_xml_generated")),
        "junit_xml_error": result.get("junit_xml_error"),
        "result_source": result.get("result_source"),
        "parser_warning": result.get("parser_warning"),
        "poll_count": poll_count,
        "wait_duration_ms": wait_duration_ms,
        "reused_existing_operation": reused_existing_operation,
        "raw_result_available_via": "get_gut_run_status",
    }
    if not passed and include_failure_output:
        stdout_tail = _trim_tail(result.get("stdout"), failure_output_limit)
        stderr_tail = _trim_tail(result.get("stderr"), failure_output_limit)
        if stdout_tail:
            payload["stdout_tail"] = stdout_tail
        if stderr_tail:
            payload["stderr_tail"] = stderr_tail
    return json.dumps(payload, ensure_ascii=False)


async def _wait_operation(
    *,
    operation: dict[str, Any],
    operation_id: str,
    control_url: str,
    api_key: str,
    worktree_name: str,
    selection_label: str | None,
    requested_wait: int,
    effective_wait: int,
    poll_interval: float,
    http_timeout: float,
    failure_output_limit: int,
    max_status_errors: int,
    include_failure_output: bool,
    reused_existing_operation: bool,
    fingerprint: str | None = None,
) -> str:
    if operation_id in _ACTIVE_WAITERS:
        return _ok(
            reason="WAITER_ALREADY_ACTIVE",
            status=str(operation.get("status") or "running"),
            terminal=False,
            operation_id=operation_id,
            worktree_name=worktree_name,
            selection=selection_label or operation.get("selection"),
            rerun_started=False,
            next_tool="gwrm_gut_wait_existing",
            safe_recovery="Do not start another GUT run. Wait for the active waiter to return or call wait_existing later.",
        )

    _ACTIVE_WAITERS.add(operation_id)
    started = time.monotonic()
    poll_count = 0
    consecutive_status_errors = 0
    try:
        while True:
            elapsed = time.monotonic() - started
            if bool(operation.get("terminal")):
                result_text = _terminal_payload(
                    operation,
                    poll_count=poll_count,
                    wait_duration_ms=int(elapsed * 1000),
                    include_failure_output=include_failure_output,
                    failure_output_limit=failure_output_limit,
                    reused_existing_operation=reused_existing_operation,
                )
                try:
                    decoded = json.loads(result_text)
                    _audit({
                        "event": "terminal",
                        "operation_id": operation_id,
                        "worktree_name": worktree_name,
                        "status": decoded.get("status"),
                        "passed": decoded.get("passed"),
                        "poll_count": poll_count,
                        "wait_duration_ms": int(elapsed * 1000),
                    })
                    if decoded.get("ok") is True and decoded.get("passed") is False and selection_label:
                        _remember_terminal_failure(worktree_name, selection_label, fingerprint, decoded)
                except Exception:
                    pass
                return result_text

            if elapsed >= effective_wait:
                _audit({
                    "event": "wait_window_expired",
                    "operation_id": operation_id,
                    "worktree_name": worktree_name,
                    "last_status": operation.get("status"),
                    "poll_count": poll_count,
                    "wait_duration_ms": int(elapsed * 1000),
                    "requested_wait_seconds": requested_wait,
                    "effective_wait_seconds": effective_wait,
                })
                return _ok(
                    reason="WAIT_WINDOW_EXPIRED",
                    status=str(operation.get("status") or "running"),
                    terminal=False,
                    operation_id=operation_id,
                    worktree_name=worktree_name,
                    selection=selection_label or operation.get("selection"),
                    last_gwrm_status=operation.get("status"),
                    poll_count=poll_count,
                    wait_duration_ms=int(elapsed * 1000),
                    requested_wait_seconds=requested_wait,
                    effective_wait_seconds=effective_wait,
                    rerun_started=False,
                    next_tool="gwrm_gut_wait_existing",
                    safe_recovery=(
                        "Continue only with gwrm_gut_wait_existing(operation_id). "
                        "Do not call gwrm_gut_run_and_wait again for the same running operation."
                    ),
                )

            sleep_for = min(poll_interval, max(0.0, effective_wait - elapsed))
            await asyncio.sleep(sleep_for)
            remaining = max(1.0, effective_wait - (time.monotonic() - started))
            request_timeout = min(http_timeout, remaining)
            try:
                operation = await _post_tool_call(
                    control_url,
                    api_key,
                    "get_gut_run_status",
                    {"operation_id": operation_id},
                    request_timeout,
                )
                poll_count += 1
                consecutive_status_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_status_errors += 1
                if consecutive_status_errors > max_status_errors:
                    elapsed = time.monotonic() - started
                    health = await _get_health(control_url, min(3.0, http_timeout))
                    gwrm_ready = bool(health.get("ready"))
                    reason = "STATUS_UNAVAILABLE" if gwrm_ready else "GWRM_UNAVAILABLE"
                    _audit({
                        "event": "status_unavailable",
                        "reason": reason,
                        "operation_id": operation_id,
                        "worktree_name": worktree_name,
                        "consecutive_errors": consecutive_status_errors,
                        "gwrm_ready": gwrm_ready,
                        "error": str(exc)[:1000],
                    })
                    return _err(
                        f"GWRM status unavailable after {consecutive_status_errors} attempts: {exc}",
                        reason=reason,
                        status="status_unavailable",
                        terminal=False,
                        operation_id=operation_id,
                        worktree_name=worktree_name,
                        poll_count=poll_count,
                        wait_duration_ms=int(elapsed * 1000),
                        gwrm_ready=gwrm_ready,
                        rerun_started=False,
                        safe_recovery=(
                            "Do not start another GUT run. Preserve WIP and recover GWRM if unavailable; "
                            "then continue this operation_id with gwrm_gut_wait_existing."
                        ),
                    )
                await asyncio.sleep(min(poll_interval, 2.0 * consecutive_status_errors))
    except asyncio.CancelledError:
        _audit({
            "event": "tool_cancelled",
            "operation_id": operation_id,
            "worktree_name": worktree_name,
            "poll_count": poll_count,
        })
        # Do not terminate the underlying GUT. GWRM remains its owner.
        raise
    finally:
        _ACTIVE_WAITERS.discard(operation_id)


async def _run_and_wait_handler(args: dict[str, Any], **_: Any) -> str:
    args = args if isinstance(args, dict) else {}
    worktree_name = _derive_worktree(args.get("worktree_name"))
    test_directory = str(args.get("test_directory") or "").strip()
    test_script = str(args.get("test_script") or "").strip()

    if not worktree_name:
        return _err("worktree_name is required and could not be derived from the current Kanban context")
    if bool(test_directory) == bool(test_script):
        return _err("provide exactly one of test_directory or test_script", worktree_name=worktree_name)
    if test_directory and not test_directory.startswith("res://"):
        return _err("test_directory must be a res:// path", worktree_name=worktree_name)
    if test_script and not test_script.startswith("res://"):
        return _err("test_script must be a res:// path", worktree_name=worktree_name)

    control_url, api_key = _resolve_runtime()
    if not api_key:
        return _err("GWRM_API_KEY is not configured for the active Hermes profile", worktree_name=worktree_name)

    cfg = _plugin_cfg()
    requested_wait, effective_wait, poll_interval, http_timeout, failure_output_limit, max_status_errors = _wait_settings(args, cfg)
    include_failure_output = bool(args.get("include_failure_output", True))
    force_rerun = bool(args.get("force_rerun", False))
    selection_label = _selection_key(test_directory, test_script)
    fingerprint = _git_state_fingerprint(worktree_name)

    if not force_rerun:
        cached = _cached_terminal_failure(worktree_name, selection_label, fingerprint)
        if cached:
            payload = dict(cached["payload"])
            payload.update({
                "ok": True,
                "reason": "REPEATED_TERMINAL_RESULT",
                "repeated_terminal_result": True,
                "rerun_started": False,
                "cached_at": cached.get("cached_at"),
                "safe_recovery": (
                    "The same selection already failed on the same Git state. "
                    "Diagnose/change state before rerun, or set force_rerun=true only with explicit reason."
                ),
            })
            _audit({
                "event": "terminal_cache_hit",
                "worktree_name": worktree_name,
                "selection": selection_label,
                "operation_id": payload.get("operation_id"),
            })
            return json.dumps(payload, ensure_ascii=False)

    start_tool = "run_gut_test_script" if test_script else "run_gut_tests"
    start_args: dict[str, Any] = {"worktree_name": worktree_name}
    if test_script:
        start_args["test_script"] = test_script
    else:
        start_args["test_directory"] = test_directory

    operation: dict[str, Any] | None = None
    start_error: Exception | None = None
    for attempt in range(2):
        try:
            operation = await _post_tool_call(control_url, api_key, start_tool, start_args, http_timeout)
            start_error = None
            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            start_error = exc
            if attempt == 0:
                await asyncio.sleep(0.5)

    if operation is None:
        message = str(start_error or "unknown GWRM start failure")
        health = await _get_health(control_url, min(3.0, http_timeout))
        reason = "GWRM_UNAVAILABLE" if not bool(health.get("ready")) else "START_FAILED"
        _audit({
            "event": "start_failed",
            "reason": reason,
            "worktree_name": worktree_name,
            "selection": selection_label,
            "error": message[:1000],
        })
        return _err(
            f"could not start GWRM GUT operation: {message}",
            reason=reason,
            worktree_name=worktree_name,
            selection=selection_label,
            gwrm_ready=bool(health.get("ready")),
            rerun_started=False,
        )

    operation_id = str(operation.get("operation_id") or "")
    if not operation_id:
        return _err(
            "GWRM start response did not include operation_id",
            reason="MALFORMED_START_RESULT",
            worktree_name=worktree_name,
            start_status=operation.get("status"),
        )

    reused = bool(operation.get("reused_existing_operation"))
    _audit({
        "event": "started",
        "operation_id": operation_id,
        "worktree_name": worktree_name,
        "selection": selection_label,
        "reused_existing_operation": reused,
        "requested_wait_seconds": requested_wait,
        "effective_wait_seconds": effective_wait,
        "poll_interval_seconds": poll_interval,
    })

    return await _wait_operation(
        operation=operation,
        operation_id=operation_id,
        control_url=control_url,
        api_key=api_key,
        worktree_name=worktree_name,
        selection_label=selection_label,
        requested_wait=requested_wait,
        effective_wait=effective_wait,
        poll_interval=poll_interval,
        http_timeout=http_timeout,
        failure_output_limit=failure_output_limit,
        max_status_errors=max_status_errors,
        include_failure_output=include_failure_output,
        reused_existing_operation=reused,
        fingerprint=fingerprint,
    )


async def _wait_existing_handler(args: dict[str, Any], **_: Any) -> str:
    args = args if isinstance(args, dict) else {}
    operation_id = str(args.get("operation_id") or "").strip()
    if not operation_id:
        return _err("operation_id is required", reason="MISSING_OPERATION_ID")

    worktree_name = _derive_worktree(args.get("worktree_name")) or ""
    control_url, api_key = _resolve_runtime()
    if not api_key:
        return _err("GWRM_API_KEY is not configured for the active Hermes profile")

    cfg = _plugin_cfg()
    requested_wait, effective_wait, poll_interval, http_timeout, failure_output_limit, max_status_errors = _wait_settings(args, cfg)
    include_failure_output = bool(args.get("include_failure_output", True))

    try:
        operation = await _post_tool_call(
            control_url,
            api_key,
            "get_gut_run_status",
            {"operation_id": operation_id},
            http_timeout,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        health = await _get_health(control_url, min(3.0, http_timeout))
        reason = "GWRM_UNAVAILABLE" if not bool(health.get("ready")) else "STATUS_UNAVAILABLE"
        return _err(
            f"could not query existing GWRM operation: {exc}",
            reason=reason,
            terminal=False,
            operation_id=operation_id,
            worktree_name=worktree_name or None,
            gwrm_ready=bool(health.get("ready")),
            rerun_started=False,
        )

    worktree_name = worktree_name or str(operation.get("worktree_name") or "")
    raw_selection = operation.get("selection")
    selection_label = None
    if isinstance(raw_selection, dict):
        selection_type = str(raw_selection.get("type") or "")
        selection_value = str(raw_selection.get("value") or "")
        if selection_type and selection_value:
            selection_label = f"{selection_type}:{selection_value}"

    fingerprint = _git_state_fingerprint(worktree_name) if worktree_name else None
    _audit({
        "event": "wait_existing_started",
        "operation_id": operation_id,
        "worktree_name": worktree_name,
        "requested_wait_seconds": requested_wait,
        "effective_wait_seconds": effective_wait,
    })

    return await _wait_operation(
        operation=operation,
        operation_id=operation_id,
        control_url=control_url,
        api_key=api_key,
        worktree_name=worktree_name,
        selection_label=selection_label,
        requested_wait=requested_wait,
        effective_wait=effective_wait,
        poll_interval=poll_interval,
        http_timeout=http_timeout,
        failure_output_limit=failure_output_limit,
        max_status_errors=max_status_errors,
        include_failure_output=include_failure_output,
        reused_existing_operation=True,
        fingerprint=fingerprint,
    )


def _available() -> bool:
    _control_url, api_key = _resolve_runtime()
    return bool(api_key)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="gwrm_gut_run_and_wait",
        toolset=TOOLSET,
        schema=RUN_SCHEMA,
        handler=_run_and_wait_handler,
        check_fn=_available,
        is_async=True,
        description="Start/reuse one GUT operation and wait inside a safe external-tool window.",
        emoji="🧪",
    )
    ctx.register_tool(
        name="gwrm_gut_wait_existing",
        toolset=TOOLSET,
        schema=WAIT_SCHEMA,
        handler=_wait_existing_handler,
        check_fn=_available,
        is_async=True,
        description="Wait for an existing GWRM GUT operation without starting another run.",
        emoji="⏳",
    )
