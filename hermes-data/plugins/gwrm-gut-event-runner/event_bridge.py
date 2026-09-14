"""Authenticated GWRM terminal-event bridge for Hermes Kanban."""
from __future__ import annotations

import hmac
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

STATE_DB = Path("/opt/data/logs/gwrm-gut-event-runner/state-v1.sqlite3")
TOKEN_FILE = Path("/opt/data/logs/gwrm-gut-event-runner/event-token")
HOST = "0.0.0.0"
PORT = int(os.environ.get("HERMES_GUT_EVENT_BRIDGE_PORT", "8653"))
# HERMES_GUT_EVENT_RECONCILE_2026_09_14
CONTROL_URL = str(
    os.environ.get("GWRM_CONTROL_URL")
    or "http://host.docker.internal:8130"
).rstrip("/")
API_KEY = str(os.environ.get("GWRM_API_KEY") or "").strip()
RECONCILE_INTERVAL_SECONDS = max(
    5.0,
    float(os.environ.get("HERMES_GUT_RECONCILE_INTERVAL_SECONDS", "15")),
)
RECONCILE_GRACE_SECONDS = max(
    0,
    int(os.environ.get("HERMES_GUT_RECONCILE_GRACE_SECONDS", "5")),
)
_OPENER = build_opener(ProxyHandler({}))


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
        CREATE TABLE IF NOT EXISTS terminal_events (
            operation_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            received_at INTEGER NOT NULL
        );
        """
    )
    return conn


def _token() -> str:
    return TOKEN_FILE.read_text(encoding="utf-8").strip()


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("content-type", "application/json; charset=utf-8")
    handler.send_header("content-length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _resume_wait(operation_id: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    conn = _db()
    try:
        now = int(time.time())
        with conn:
            conn.execute(
                """
                INSERT INTO terminal_events(operation_id, payload_json, received_at)
                VALUES (?, ?, ?)
                ON CONFLICT(operation_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    received_at=excluded.received_at
                """,
                (operation_id, json.dumps(payload, ensure_ascii=False), now),
            )
        wait = conn.execute(
            "SELECT * FROM waits WHERE operation_id = ?",
            (operation_id,),
        ).fetchone()
        if wait is None:
            return 409, {
                "ok": False,
                "reason": "WAIT_NOT_REGISTERED",
                "operation_id": operation_id,
            }

        from hermes_cli import kanban_db as kb

        old_home = os.environ.get("HERMES_HOME")
        try:
            if wait["hermes_home"]:
                os.environ["HERMES_HOME"] = str(wait["hermes_home"])
            board = str(wait["board"]) if wait["board"] else None
            kb_conn = kb.connect(board=board)
            try:
                task = kb.get_task(kb_conn, str(wait["task_id"]))
                if task is None:
                    return 410, {
                        "ok": False,
                        "reason": "TASK_NOT_FOUND",
                        "operation_id": operation_id,
                    }

                status = str(getattr(task, "status", "") or "")
                if status in {"ready", "todo", "review"} and str(wait["state"]) in {"resumed", "collected"}:
                    return 200, {
                        "ok": True,
                        "reason": "ALREADY_RESUMED",
                        "operation_id": operation_id,
                        "task_id": str(wait["task_id"]),
                    }

                if status not in {"blocked", "scheduled"}:
                    return 409, {
                        "ok": False,
                        "reason": "TASK_NOT_PARKED_YET",
                        "operation_id": operation_id,
                        "task_id": str(wait["task_id"]),
                        "task_status": status,
                    }

                changed = kb.unblock_task(kb_conn, str(wait["task_id"]))
                if not changed:
                    return 409, {
                        "ok": False,
                        "reason": "UNBLOCK_REFUSED",
                        "operation_id": operation_id,
                        "task_id": str(wait["task_id"]),
                    }

                result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
                counts = result.get("counts") if isinstance(result.get("counts"), dict) else {}
                summary = {
                    "operation_id": operation_id,
                    "status": payload.get("status"),
                    "passed": result.get("passed"),
                    "counts": counts,
                    "exit_code": result.get("exit_code"),
                    "timed_out": result.get("timed_out"),
                    "duration_ms": result.get("duration_ms"),
                    "result_source": result.get("result_source"),
                    "junit_xml_generated": result.get("junit_xml_generated"),
                }
                body = (
                    "GWRM_GUT_TERMINAL_EVENT\n"
                    f"operation_id: {operation_id}\n"
                    f"event_driven: true\n"
                    f"result: {json.dumps(summary, ensure_ascii=False, sort_keys=True)}\n"
                    "next_action: call gwrm_gut_collect_event_result exactly once; do not poll GWRM.\n"
                )
                try:
                    kb.add_comment(
                        kb_conn,
                        str(wait["task_id"]),
                        author="gwrm-gut-event-bridge",
                        body=body,
                    )
                except Exception:
                    pass
            finally:
                kb_conn.close()
        finally:
            if old_home is None:
                os.environ.pop("HERMES_HOME", None)
            else:
                os.environ["HERMES_HOME"] = old_home

        with conn:
            conn.execute(
                "UPDATE waits SET state='resumed', updated_at=? WHERE operation_id=?",
                (now, operation_id),
            )

        return 200, {
            "ok": True,
            "reason": "CARD_RESUMED",
            "operation_id": operation_id,
            "task_id": str(wait["task_id"]),
        }
    finally:
        conn.close()


def _gwrm_status(operation_id: str) -> dict[str, Any]:
    if not API_KEY:
        raise RuntimeError("GWRM_API_KEY missing from event bridge environment")

    body = json.dumps(
        {
            "name": "get_gut_run_status",
            "arguments": {"operation_id": operation_id},
        },
        ensure_ascii=False,
    ).encode("utf-8")

    request = Request(
        f"{CONTROL_URL}/api/v1/tools/call",
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "X-API-Key": API_KEY,
        },
    )

    try:
        with _OPENER.open(request, timeout=10.0) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"GWRM HTTP {exc.code}: {detail or exc.reason}"
        ) from exc

    if not isinstance(payload, dict):
        raise RuntimeError("GWRM status response is not an object")

    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError(
            str(payload.get("error") or "GWRM status response missing result")
        )

    return result


def _reconcile_waits_once() -> dict[str, int]:
    stats = {
        "checked": 0,
        "terminal": 0,
        "resumed": 0,
        "errors": 0,
    }

    if not API_KEY:
        return stats

    conn = _db()
    try:
        cutoff = int(time.time()) - RECONCILE_GRACE_SECONDS
        waits = conn.execute(
            """
            SELECT operation_id, state, updated_at
            FROM waits
            WHERE state IN ('registered', 'parked')
              AND updated_at <= ?
            ORDER BY updated_at ASC
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    for wait in waits:
        operation_id = str(wait["operation_id"])
        stats["checked"] += 1
        try:
            payload = _gwrm_status(operation_id)
            if payload.get("terminal") is not True:
                continue

            stats["terminal"] += 1
            status, response = _resume_wait(operation_id, payload)

            if status == 200:
                stats["resumed"] += 1
                print(
                    "gwrm-gut-event-bridge reconcile "
                    f"operation_id={operation_id} "
                    f"result={response.get('reason')}",
                    flush=True,
                )
            elif (
                status == 409
                and response.get("reason") == "TASK_NOT_PARKED_YET"
            ):
                # _resume_wait stores terminal_events before checking the
                # Kanban state. The runner's race-safe path will consume it.
                print(
                    "gwrm-gut-event-bridge reconcile terminal-before-park "
                    f"operation_id={operation_id}",
                    flush=True,
                )
            else:
                print(
                    "gwrm-gut-event-bridge reconcile deferred "
                    f"operation_id={operation_id} "
                    f"http={status} reason={response.get('reason')}",
                    flush=True,
                )
        except Exception as exc:
            stats["errors"] += 1
            print(
                "gwrm-gut-event-bridge reconcile error "
                f"operation_id={operation_id} "
                f"error={type(exc).__name__}:{str(exc)[:300]}",
                flush=True,
            )

    return stats


def _reconcile_loop() -> None:
    while True:
        try:
            _reconcile_waits_once()
        except Exception as exc:
            print(
                "gwrm-gut-event-bridge reconcile loop error "
                f"{type(exc).__name__}:{str(exc)[:500]}",
                flush=True,
            )
        time.sleep(RECONCILE_INTERVAL_SECONDS)

class Handler(BaseHTTPRequestHandler):
    server_version = "HermesGutEventBridge/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        if self.path == "/health":
            _json_response(self, 200, {"ready": True, "service": "hermes-gut-event-bridge"})
            return
        _json_response(self, 404, {"ok": False, "reason": "NOT_FOUND"})

    def do_POST(self) -> None:
        if self.path != "/v1/gut-terminal":
            _json_response(self, 404, {"ok": False, "reason": "NOT_FOUND"})
            return

        expected = _token()
        auth = self.headers.get("Authorization", "")
        supplied = auth[7:].strip() if auth.startswith("Bearer ") else ""
        if not expected or not hmac.compare_digest(expected, supplied):
            _json_response(self, 401, {"ok": False, "reason": "UNAUTHORIZED"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(max(0, min(length, 2_000_000)))
            payload = json.loads(raw.decode("utf-8"))
        except Exception:
            _json_response(self, 400, {"ok": False, "reason": "INVALID_JSON"})
            return

        if not isinstance(payload, dict):
            _json_response(self, 400, {"ok": False, "reason": "INVALID_PAYLOAD"})
            return

        operation_id = str(payload.get("operation_id") or "").strip()
        if not operation_id.startswith("gut_") or payload.get("terminal") is not True:
            _json_response(self, 400, {"ok": False, "reason": "INVALID_TERMINAL_EVENT"})
            return

        try:
            status, response = _resume_wait(operation_id, payload)
        except Exception as exc:
            _json_response(
                self,
                500,
                {
                    "ok": False,
                    "reason": "BRIDGE_ERROR",
                    "error": f"{type(exc).__name__}: {exc}"[:1000],
                },
            )
            return

        _json_response(self, status, response)


def main() -> None:
    if not TOKEN_FILE.exists() or not _token():
        raise SystemExit(f"missing event token: {TOKEN_FILE}")
    _db().close()
    reconcile_thread = threading.Thread(
        target=_reconcile_loop,
        name="gwrm-gut-event-reconcile",
        daemon=True,
    )
    reconcile_thread.start()
    server = HTTPServer((HOST, PORT), Handler)
    print(f"hermes-gut-event-bridge listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
