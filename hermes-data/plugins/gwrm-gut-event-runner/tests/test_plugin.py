from __future__ import annotations

import importlib.util
import json
from pathlib import Path


PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"
spec = importlib.util.spec_from_file_location("gwrm_gut_event_runner_under_test", PLUGIN)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def test_selection_requires_exactly_one():
    assert module._selection({}) is None
    assert module._selection({"test_directory": "res://tests", "test_script": "res://x.gd"}) is None
    assert module._selection({"test_directory": "res://tests"}) == ("directory", "res://tests")
    assert module._selection({"test_script": "res://x.gd"}) == ("script", "res://x.gd")


def test_compact_pass():
    payload = {
        "operation_id": "gut_x",
        "status": "completed",
        "terminal": True,
        "result": {
            "passed": True,
            "counts": {"tests": 3, "asserts": 8, "failing_tests": 0, "errors": 0},
            "exit_code": 0,
            "duration_ms": 10,
            "junit_xml_generated": True,
            "result_source": "junit_xml",
            "stdout": "secret pass output",
        },
    }
    result = module._compact_terminal(payload)
    assert result["ok"] is True
    assert result["reason"] == "TESTS_PASSED"
    assert result["passed"] is True
    assert "stdout_tail" not in result


def test_compact_verified_failure():
    payload = {
        "operation_id": "gut_x",
        "status": "completed",
        "terminal": True,
        "result": {
            "passed": False,
            "counts": {"tests": 1, "asserts": 2, "failing_tests": 1, "errors": 0},
            "exit_code": 1,
            "junit_xml_generated": True,
            "result_source": "junit_xml",
            "stderr": "assert failed",
        },
    }
    result = module._compact_terminal(payload)
    assert result["ok"] is True
    assert result["reason"] == "TESTS_FAILED"
    assert result["passed"] is False
    assert result["stderr_tail"] == "assert failed"


def test_compact_unverified_red():
    payload = {
        "operation_id": "gut_x",
        "status": "completed",
        "terminal": True,
        "result": {
            "passed": False,
            "counts": {
                "tests": None,
                "asserts": None,
                "failing_tests": 0,
                "errors": 0,
            },
            "exit_code": 1,
            "junit_xml_generated": False,
            "result_source": "stdout_fallback",
        },
    }
    result = module._compact_terminal(payload)
    assert result["ok"] is False
    assert result["reason"] == "GUT_RESULT_UNVERIFIED"


def test_source_has_no_polling_primitives():
    source = PLUGIN.read_text(encoding="utf-8")
    assert "asyncio.sleep" not in source
    assert "get_gut_run_status" not in source
    assert "WAIT_WINDOW_EXPIRED" not in source
