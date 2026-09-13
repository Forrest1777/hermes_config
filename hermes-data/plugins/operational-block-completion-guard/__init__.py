from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


_MARKER = "BLOCKED_OPERATIONAL"

_MARKER_RE = re.compile(
    r"^\s*(?:[#>*`_\-\s]*)BLOCKED_OPERATIONAL\b",
    re.IGNORECASE,
)

_METADATA_KEYS = {
    "status",
    "outcome",
    "state",
    "result",
    "completion_status",
    "handoff_status",
}

_ORIGINAL_COMPLETE = None


def _audit(payload: dict[str, Any]) -> None:
    try:
        path = Path(
            "/opt/data/logs/"
            "operational-block-completion-guard/"
            "events.jsonl"
        )

        path.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        record = {
            "ts": int(time.time()),
            **payload,
        }

        with path.open(
            "a",
            encoding="utf-8",
        ) as stream:
            stream.write(
                json.dumps(
                    record,
                    ensure_ascii=False,
                    sort_keys=True,
                )
                + "\n"
            )
    except Exception:
        pass


def _text_declares_block(value: Any) -> bool:
    if value is None:
        return False

    text = str(value)

    for line in text.splitlines():
        if not line.strip():
            continue

        # Deliberately inspect only the first
        # non-empty line. A later sentence merely
        # mentioning BLOCKED_OPERATIONAL must not
        # turn a legitimate success into a block.
        return bool(
            _MARKER_RE.match(line)
        )

    return False


def _normalize_scalar(value: Any) -> str:
    return (
        str(value)
        .strip()
        .upper()
        .replace("-", "_")
        .replace(" ", "_")
    )


def _metadata_declares_block(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized_key = (
                str(key)
                .strip()
                .lower()
            )

            if (
                normalized_key in _METADATA_KEYS
                and not isinstance(
                    item,
                    (dict, list, tuple),
                )
                and _normalize_scalar(item)
                == _MARKER
            ):
                return True

            if _metadata_declares_block(item):
                return True

    elif isinstance(value, (list, tuple)):
        for item in value:
            if _metadata_declares_block(item):
                return True

    return False


def _declares_blocked_operational(
    *,
    summary: Any,
    result: Any,
    metadata: Any,
) -> bool:
    return (
        _text_declares_block(summary)
        or _text_declares_block(result)
        or _metadata_declares_block(metadata)
    )


def _guarded_complete(
    conn,
    task_id,
    *args,
    **kwargs,
):
    summary = kwargs.get("summary")
    result = kwargs.get("result")
    metadata = kwargs.get("metadata")

    if not _declares_blocked_operational(
        summary=summary,
        result=result,
        metadata=metadata,
    ):
        return _ORIGINAL_COMPLETE(
            conn,
            task_id,
            *args,
            **kwargs,
        )

    from hermes_cli import kanban_db as kb

    expected_run_id = kwargs.get(
        "expected_run_id"
    )

    reason = (
        "BLOCKED_OPERATIONAL: kanban_complete "
        "was intercepted because the handoff "
        "declares an operational block. "
        "The task was kept non-terminal and "
        "dependencies must remain unsatisfied. "
        "Recovery/orchestrator intervention is "
        "required before execution can continue."
    )

    blocked = kb.block_task(
        conn,
        task_id,
        reason=reason,
        kind="needs_input",
        expected_run_id=expected_run_id,
    )

    if not blocked:
        _audit({
            "event":
                "operational_block_conversion_failed",
            "task_id": task_id,
        })

        return False

    try:
        kb.add_comment(
            conn,
            task_id,
            author=
                "operational-block-completion-guard",
            body=(
                "BLOCKED_OPERATIONAL_INTERCEPTED\n"
                "attempted_operation: kanban_complete\n"
                "result: converted_to_blocked\n"
                "block_kind: needs_input\n"
                "dependency_satisfaction: refused\n"
                "required_action: orchestrator/recovery "
                "must inspect the operational block "
                "before the task is resumed.\n"
            ),
        )
    except Exception as exc:
        _audit({
            "event": "comment_failed",
            "task_id": task_id,
            "error":
                f"{type(exc).__name__}: {exc}",
        })

    _audit({
        "event":
            "blocked_operational_intercepted",
        "task_id": task_id,
        "expected_run_id": expected_run_id,
    })

    # False is intentional. kanban_complete must
    # report that completion did NOT occur.
    return False


_guarded_complete._operational_block_guard = True


def _ensure_patch() -> bool:
    global _ORIGINAL_COMPLETE

    from hermes_cli import kanban_db as kb

    current = kb.complete_task

    if getattr(
        current,
        "_operational_block_guard",
        False,
    ):
        _ORIGINAL_COMPLETE = getattr(
            current,
            "_operational_block_original",
            None,
        ) or _ORIGINAL_COMPLETE

        return True

    _ORIGINAL_COMPLETE = current

    _guarded_complete._operational_block_original = (
        current
    )

    kb.complete_task = _guarded_complete

    _audit({
        "event": "complete_task_patched",
    })

    return True


def _health_hook(**kwargs):
    del kwargs
    return _ensure_patch()


def register(ctx):
    _ensure_patch()

    ctx.register_hook(
        "on_kanban_dispatch_tick",
        _health_hook,
    )
# HERMES_CANONICAL_LOCAL_DELIVERY_2026_09_13:BEGIN
# Root completion gate: a root-like canonical checkpoint is terminal only after
# verified local delivery to main. Non-root tasks remain unaffected.
import yaml as _delivery_yaml


def _delivery_checkpoint_state(conn, task_id):
    rows = conn.execute(
        "SELECT id, body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 160",
        (task_id,),
    ).fetchall()
    for row in rows:
        body = str(row["body"] or "").strip()
        if "OPERATIONAL_CHECKPOINT_CANONICAL" not in body:
            continue
        lines = body.splitlines()
        if lines and lines[0].startswith("OPERATIONAL_CHECKPOINT_CANONICAL"):
            lines = lines[1:]
        try:
            parsed = _delivery_yaml.safe_load("\n".join(lines))
        except Exception:
            continue
        if not isinstance(parsed, dict):
            continue
        cp = parsed.get("operational_checkpoint") if isinstance(parsed.get("operational_checkpoint"), dict) else parsed
        if not isinstance(cp, dict):
            continue
        root_branch = str(cp.get("root_integration_branch") or cp.get("integration_target_branch") or "").strip()
        phase_id = str(cp.get("phase_id") or "").strip()
        if not root_branch or not phase_id:
            continue
        delivery = cp.get("delivery_state") if isinstance(cp.get("delivery_state"), dict) else {}
        target = str(cp.get("delivery_target_branch") or delivery.get("delivery_target_branch") or "").strip()
        ready = (
            target == "main"
            and delivery.get("main_integrated") is True
            and delivery.get("main_integration_pending") is False
            and delivery.get("push_performed") in (False, None)
        )
        return {
            "applicable": True,
            "ready": ready,
            "checkpoint_comment_id": int(row["id"]),
            "phase_id": phase_id,
            "root_integration_branch": root_branch,
            "delivery_target_branch": target,
            "delivery_state": delivery,
        }
    return {"applicable": False, "ready": False}


_ORIGINAL_BLOCK_GUARDED_COMPLETE = _guarded_complete


def _delivery_guarded_complete(conn, task_id, *args, **kwargs):
    state = _delivery_checkpoint_state(conn, task_id)
    if state.get("applicable") and not state.get("ready"):
        from hermes_cli import kanban_db as kb
        expected_run_id = kwargs.get("expected_run_id")
        reason = (
            "BLOCKED_OPERATIONAL: root completion refused because canonical local delivery "
            "to main is not verified. Run operational_sync_finalize and then "
            "local_main_delivery_finalize; push remains human-only."
        )
        blocked = kb.block_task(
            conn,
            task_id,
            reason=reason,
            kind="needs_input",
            expected_run_id=expected_run_id,
        )
        if blocked:
            try:
                kb.add_comment(
                    conn,
                    task_id,
                    author="operational-block-completion-guard",
                    body=(
                        "CANONICAL_LOCAL_DELIVERY_REQUIRED\n"
                        f"checkpoint_comment_id: {state.get('checkpoint_comment_id')}\n"
                        f"phase_id: {state.get('phase_id')}\n"
                        f"root_integration_branch: {state.get('root_integration_branch')}\n"
                        f"delivery_target_branch: {state.get('delivery_target_branch')}\n"
                        "main_integrated: false_or_unverified\n"
                        "push_performed: false\n"
                    ),
                )
            except Exception:
                pass
        _audit({
            "event": "canonical_local_delivery_completion_refused",
            "task_id": task_id,
            "state": state,
        })
        return False
    return _ORIGINAL_BLOCK_GUARDED_COMPLETE(conn, task_id, *args, **kwargs)


_delivery_guarded_complete._operational_block_guard = True
_delivery_guarded_complete._operational_block_original = getattr(
    _ORIGINAL_BLOCK_GUARDED_COMPLETE,
    "_operational_block_original",
    None,
)
_guarded_complete = _delivery_guarded_complete
# HERMES_CANONICAL_LOCAL_DELIVERY_2026_09_13:END
