from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("gwrm_gut_runner_under_test", PLUGIN)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def run(coro):
    return asyncio.run(coro)


def _disable_real_git(monkeypatch):
    monkeypatch.setattr(module, "_git_state_fingerprint", lambda _wt: "fp-test")


def test_requires_exactly_one_selection(monkeypatch):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    result = json.loads(run(module._run_and_wait_handler({"worktree_name": "t_abc"})))
    assert result["ok"] is False
    assert "exactly one" in result["error"]


def test_terminal_pass_compacts_output(monkeypatch):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    _disable_real_git(monkeypatch)
    calls = []

    async def fake_call(control_url, api_key, name, arguments, timeout_seconds):
        calls.append(name)
        if name == "run_gut_tests":
            return {
                "operation_id": "gut_test",
                "worktree_name": "t_abc",
                "selection": {"type": "directory", "value": "res://tests"},
                "status": "queued",
                "terminal": False,
                "reused_existing_operation": False,
            }
        return {
            "operation_id": "gut_test",
            "worktree_name": "t_abc",
            "selection": {"type": "directory", "value": "res://tests"},
            "status": "completed",
            "terminal": True,
            "result": {
                "passed": True,
                "counts": {"tests": 3, "asserts": 10, "failing_tests": 0, "errors": 0},
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 123,
                "junit_xml_generated": True,
                "result_source": "junit_xml",
                "stdout": "SHOULD NOT LEAK ON PASS",
                "stderr": "",
            },
        }

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(module, "_post_tool_call", fake_call)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    result = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_directory": "res://tests",
        "poll_interval_seconds": 1,
    })))
    assert result["ok"] is True
    assert result["passed"] is True
    assert result["reason"] == "TESTS_PASSED"
    assert "stdout_tail" not in result
    assert calls == ["run_gut_tests", "get_gut_run_status"]


def test_terminal_test_failure_is_tool_success(monkeypatch, tmp_path):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    _disable_real_git(monkeypatch)
    monkeypatch.setattr(module, "CACHE_PATH", tmp_path / "cache.json")

    async def fake_call(control_url, api_key, name, arguments, timeout_seconds):
        if name == "run_gut_test_script":
            return {
                "operation_id": "gut_fail",
                "worktree_name": "t_abc",
                "selection": {"type": "script", "value": "res://tests/test_x.gd"},
                "status": "running",
                "terminal": False,
                "reused_existing_operation": False,
            }
        return {
            "operation_id": "gut_fail",
            "worktree_name": "t_abc",
            "selection": {"type": "script", "value": "res://tests/test_x.gd"},
            "status": "completed",
            "terminal": True,
            "result": {
                "passed": False,
                "counts": {"tests": 1, "asserts": 2, "failing_tests": 1, "errors": 0},
                "failure": [{"message": "expected true"}],
                "exit_code": 1,
                "timed_out": False,
                "duration_ms": 100,
                "junit_xml_generated": True,
                "result_source": "junit_xml",
                "stdout": "x" * 10000,
                "stderr": "failure detail",
            },
        }

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(module, "_post_tool_call", fake_call)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    result = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_script": "res://tests/test_x.gd",
        "poll_interval_seconds": 1,
    })))
    assert result["ok"] is True
    assert result["passed"] is False
    assert result["reason"] == "TESTS_FAILED"
    assert len(result["stdout_tail"]) < 3200
    assert result["stderr_tail"] == "failure detail"


def test_reuses_existing_operation(monkeypatch):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    _disable_real_git(monkeypatch)

    async def fake_call(control_url, api_key, name, arguments, timeout_seconds):
        if name == "run_gut_tests":
            return {
                "operation_id": "gut_existing",
                "worktree_name": "t_abc",
                "status": "running",
                "terminal": False,
                "reused_existing_operation": True,
            }
        return {
            "operation_id": "gut_existing",
            "worktree_name": "t_abc",
            "status": "completed",
            "terminal": True,
            "result": {
                "passed": True,
                "counts": {"tests": 1, "asserts": 1, "failing_tests": 0, "errors": 0},
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 1,
                "junit_xml_generated": True,
                "result_source": "junit_xml",
            },
        }

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(module, "_post_tool_call", fake_call)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)
    result = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_directory": "res://tests",
    })))
    assert result["ok"] is True
    assert result["reused_existing_operation"] is True


def test_wait_existing_never_starts_gut(monkeypatch):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    _disable_real_git(monkeypatch)
    calls = []

    async def fake_call(control_url, api_key, name, arguments, timeout_seconds):
        calls.append(name)
        return {
            "operation_id": "gut_existing",
            "worktree_name": "t_abc",
            "selection": {"type": "script", "value": "res://tests/test_x.gd"},
            "status": "completed",
            "terminal": True,
            "result": {
                "passed": True,
                "counts": {"tests": 1, "asserts": 1, "failing_tests": 0, "errors": 0},
                "exit_code": 0,
                "timed_out": False,
                "duration_ms": 1,
                "junit_xml_generated": True,
                "result_source": "junit_xml",
            },
        }

    monkeypatch.setattr(module, "_post_tool_call", fake_call)
    result = json.loads(run(module._wait_existing_handler({
        "operation_id": "gut_existing",
        "worktree_name": "t_abc",
    })))
    assert result["ok"] is True
    assert result["passed"] is True
    assert calls == ["get_gut_run_status"]


def test_requested_wait_is_clamped_to_safe_window(monkeypatch):
    cfg = {
        "max_wait_seconds": 720,
        "external_tool_timeout_seconds": 420,
        "tool_timeout_safety_margin_seconds": 60,
        "poll_interval_seconds": 5,
        "http_timeout_seconds": 10,
        "max_failure_output_chars": 3000,
        "max_consecutive_status_errors": 3,
    }
    requested, effective, *_rest = module._wait_settings({"max_wait_seconds": 900}, cfg)
    assert requested == 900
    assert effective == 360


def test_terminal_failure_cache_blocks_identical_rerun(monkeypatch, tmp_path):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    monkeypatch.setattr(module, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(module, "_git_state_fingerprint", lambda _wt: "same-fp")

    start_calls = 0

    async def fake_call(control_url, api_key, name, arguments, timeout_seconds):
        nonlocal start_calls
        if name == "run_gut_tests":
            start_calls += 1
            return {
                "operation_id": "gut_fail_once",
                "worktree_name": "t_abc",
                "selection": {"type": "directory", "value": "res://tests"},
                "status": "running",
                "terminal": False,
                "reused_existing_operation": False,
            }
        return {
            "operation_id": "gut_fail_once",
            "worktree_name": "t_abc",
            "selection": {"type": "directory", "value": "res://tests"},
            "status": "completed",
            "terminal": True,
            "result": {
                "passed": False,
                "counts": {"tests": 1, "asserts": 1, "failing_tests": 1, "errors": 0},
                "exit_code": 1,
                "timed_out": False,
                "duration_ms": 1,
                "junit_xml_generated": True,
                "result_source": "junit_xml",
            },
        }

    async def fake_sleep(_seconds):
        return None

    monkeypatch.setattr(module, "_post_tool_call", fake_call)
    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    first = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_directory": "res://tests",
    })))
    second = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_directory": "res://tests",
    })))

    assert first["passed"] is False
    assert second["reason"] == "REPEATED_TERMINAL_RESULT"
    assert second["rerun_started"] is False
    assert start_calls == 1


def test_force_rerun_bypasses_terminal_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    monkeypatch.setattr(module, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(module, "_git_state_fingerprint", lambda _wt: "same-fp")

    cached_payload = {
        "ok": True,
        "reason": "TESTS_FAILED",
        "terminal": True,
        "passed": False,
        "operation_id": "old",
    }
    module._remember_terminal_failure("t_abc", "directory:res://tests", "same-fp", cached_payload)

    start_calls = 0

    async def fake_call(control_url, api_key, name, arguments, timeout_seconds):
        nonlocal start_calls
        if name == "run_gut_tests":
            start_calls += 1
            return {
                "operation_id": "new",
                "worktree_name": "t_abc",
                "selection": {"type": "directory", "value": "res://tests"},
                "status": "completed",
                "terminal": True,
                "reused_existing_operation": False,
                "result": {
                    "passed": True,
                    "counts": {"tests": 1, "asserts": 1, "failing_tests": 0, "errors": 0},
                    "exit_code": 0,
                    "timed_out": False,
                    "duration_ms": 1,
                    "junit_xml_generated": True,
                    "result_source": "junit_xml",
                },
            }
        raise AssertionError(name)

    monkeypatch.setattr(module, "_post_tool_call", fake_call)
    result = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_directory": "res://tests",
        "force_rerun": True,
    })))
    assert result["passed"] is True
    assert start_calls == 1


def test_active_waiter_returns_without_polling(monkeypatch):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    module._ACTIVE_WAITERS.add("gut_busy")
    try:
        result = json.loads(run(module._wait_operation(
            operation={"operation_id": "gut_busy", "status": "running", "terminal": False},
            operation_id="gut_busy",
            control_url="http://example",
            api_key="x",
            worktree_name="t_abc",
            selection_label="script:res://tests/test_x.gd",
            requested_wait=360,
            effective_wait=360,
            poll_interval=5,
            http_timeout=10,
            failure_output_limit=3000,
            max_status_errors=3,
            include_failure_output=True,
            reused_existing_operation=True,
        )))
        assert result["ok"] is True
        assert result["reason"] == "WAITER_ALREADY_ACTIVE"
        assert result["rerun_started"] is False
    finally:
        module._ACTIVE_WAITERS.discard("gut_busy")


def test_unverified_terminal_red_is_not_test_failure_or_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("GWRM_API_KEY", "test")
    monkeypatch.setattr(module, "CACHE_PATH", tmp_path / "cache-v2.json")
    monkeypatch.setattr(module, "_git_state_fingerprint", lambda _wt: "fp-unverified")

    start_calls = 0

    async def fake_call(control_url, api_key, name, arguments, timeout_seconds):
        nonlocal start_calls
        if name == "run_gut_test_script":
            start_calls += 1
            return {
                "operation_id": f"gut_unverified_{start_calls}",
                "worktree_name": "t_abc",
                "selection": {"type": "script", "value": "res://tests/test_x.gd"},
                "status": "completed",
                "terminal": True,
                "reused_existing_operation": False,
                "result": {
                    "passed": False,
                    "counts": {
                        "scripts": None,
                        "tests": None,
                        "passing_tests": None,
                        "failing_tests": 0,
                        "pending_tests": 0,
                        "asserts": None,
                        "errors": 0,
                        "warnings": None,
                    },
                    "failure": None,
                    "fatal_patterns": [],
                    "exit_code": 1,
                    "timed_out": False,
                    "duration_ms": 15000,
                    "junit_xml_generated": False,
                    "junit_xml_error": "ENOENT",
                    "result_source": "stdout_fallback",
                    "parser_warning": "JUnit XML missing",
                    "stdout": "GUT started test then exited",
                    "stderr": "",
                },
            }
        raise AssertionError(name)

    monkeypatch.setattr(module, "_post_tool_call", fake_call)

    first = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_script": "res://tests/test_x.gd",
    })))
    second = json.loads(run(module._run_and_wait_handler({
        "worktree_name": "t_abc",
        "test_script": "res://tests/test_x.gd",
    })))

    assert first["ok"] is False
    assert first["reason"] == "GUT_RESULT_UNVERIFIED"
    assert second["ok"] is False
    assert second["reason"] == "GUT_RESULT_UNVERIFIED"
    assert start_calls == 2
    assert not module.CACHE_PATH.exists()
