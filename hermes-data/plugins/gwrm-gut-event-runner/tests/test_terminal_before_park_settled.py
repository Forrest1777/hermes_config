from __future__ import annotations

import importlib.util
import sqlite3
import tempfile
import time
from pathlib import Path

BRIDGE = Path("/opt/data/plugins/gwrm-gut-event-runner/event_bridge.py")

spec = importlib.util.spec_from_file_location(
    "gut_event_bridge_terminal_before_park_test",
    BRIDGE,
)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)

with tempfile.TemporaryDirectory() as tmp:
    module.STATE_DB = Path(tmp) / "state.sqlite3"
    module.API_KEY = "test-key"
    module.RECONCILE_GRACE_SECONDS = 0

    now = int(time.time()) - 20
    conn = module._db()
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
                "gut_terminal_before_park_test",
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

    calls = {"status": 0, "resume": 0}

    def fake_status(operation_id):
        calls["status"] += 1
        return {
            "operation_id": operation_id,
            "terminal": True,
            "status": "completed",
            "result": {
                "passed": True,
                "counts": {"tests": 1, "failing_tests": 0},
            },
        }

    def fake_resume(operation_id, payload):
        calls["resume"] += 1

        # Mirror _resume_wait's important side effect: terminal event already
        # exists before TASK_NOT_PARKED_YET is returned.
        conn = module._db()
        try:
            with conn:
                conn.execute(
                    """
                    INSERT INTO terminal_events(
                        operation_id, payload_json, received_at
                    ) VALUES (?, ?, ?)
                    ON CONFLICT(operation_id) DO UPDATE SET
                        payload_json=excluded.payload_json,
                        received_at=excluded.received_at
                    """,
                    (operation_id, "{}", int(time.time())),
                )
        finally:
            conn.close()

        return 409, {
            "ok": False,
            "reason": "TASK_NOT_PARKED_YET",
            "operation_id": operation_id,
        }

    module._gwrm_status = fake_status
    module._resume_wait = fake_resume

    first = module._reconcile_waits_once()
    assert first["checked"] == 1, first
    assert first["terminal"] == 1, first
    assert calls["status"] == 1, calls
    assert calls["resume"] == 1, calls

    conn = module._db()
    row = conn.execute(
        "SELECT state FROM waits WHERE operation_id=?",
        ("gut_terminal_before_park_test",),
    ).fetchone()
    event = conn.execute(
        "SELECT 1 FROM terminal_events WHERE operation_id=?",
        ("gut_terminal_before_park_test",),
    ).fetchone()
    conn.close()

    assert row is not None
    assert row["state"] == "terminal_before_park", row["state"]
    assert event is not None

    # Critical regression: second reconciliation must not poll this operation.
    second = module._reconcile_waits_once()
    assert second["checked"] == 0, second
    assert calls["status"] == 1, calls
    assert calls["resume"] == 1, calls

print("[OK] terminal-before-park settles and leaves reconcile queue")