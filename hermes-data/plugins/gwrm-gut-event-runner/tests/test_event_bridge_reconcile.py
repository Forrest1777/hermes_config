from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import time
from pathlib import Path

BRIDGE = Path("/opt/data/plugins/gwrm-gut-event-runner/event_bridge.py")

spec = importlib.util.spec_from_file_location("gut_event_bridge_test", BRIDGE)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as tmp:
    module.STATE_DB = Path(tmp) / "state.sqlite3"
    module.API_KEY = "test-key"
    module.RECONCILE_GRACE_SECONDS = 0

    conn = module._db()
    now = int(time.time()) - 10
    with conn:
        conn.execute(
            """
            INSERT INTO waits (
                operation_id, task_id, run_id, profile, board, hermes_home,
                worktree_name, selection_type, selection_value, git_fingerprint,
                state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "gut_test_terminal",
                "t_test",
                1,
                "implementation-worker",
                None,
                "/opt/data/profiles/implementation-worker",
                "t_test",
                "script",
                "res://tests/test.gd",
                "fp",
                "parked",
                now,
                now,
            ),
        )
    conn.close()

    calls = []

    def fake_status(operation_id):
        assert operation_id == "gut_test_terminal"
        return {
            "operation_id": operation_id,
            "terminal": True,
            "status": "completed",
            "result": {
                "passed": False,
                "counts": {"tests": 1, "failing_tests": 1},
            },
        }

    def fake_resume(operation_id, payload):
        calls.append((operation_id, payload))
        return 200, {"ok": True, "reason": "CARD_RESUMED"}

    module._gwrm_status = fake_status
    module._resume_wait = fake_resume

    stats = module._reconcile_waits_once()

    assert stats["checked"] == 1, stats
    assert stats["terminal"] == 1, stats
    assert stats["resumed"] == 1, stats
    assert stats["errors"] == 0, stats
    assert len(calls) == 1, calls

print("[OK] lost terminal callback reconciliation")