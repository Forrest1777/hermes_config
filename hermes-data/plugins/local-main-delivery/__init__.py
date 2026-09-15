"""Fail-closed local delivery of an orchestrator root integration head to local main.

No push is ever performed. The tool is intentionally orchestrator-only and is meant
to run after operational_sync_finalize produced a clean docs head on wt/<root>.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

import yaml

NAME = "local-main-delivery"
VERSION = "1.1.0"
TOOLSET = "local_main_delivery"
ALLOWED_PROFILES = {"implementation-orchestrator"}
MARKER = "HERMES_CANONICAL_LOCAL_DELIVERY_2026_09_13"

FINALIZE_SCHEMA = {
    "name": "local_main_delivery_finalize",
    "description": (
        "Deliver the validated root integration/docs head to the local main worktree by ff-only, "
        "record post-delivery operational state, fast-forward the root branch to the same final head, "
        "and publish a canonical delivery checkpoint. Never pushes."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Short audit reason."},
        },
        "additionalProperties": False,
    },
}


def _ok(**fields: Any) -> str:
    return json.dumps({"ok": True, **fields}, ensure_ascii=False)


def _err(message: str, **fields: Any) -> str:
    return json.dumps({"ok": False, "error": message, **fields}, ensure_ascii=False)


def _profile() -> str:
    return str(os.environ.get("HERMES_PROFILE") or "")


def _available() -> bool:
    return (
        _profile() in ALLOWED_PROFILES
        and bool(os.environ.get("HERMES_KANBAN_TASK"))
        and bool(os.environ.get("HERMES_KANBAN_WORKSPACE"))
    )


def _cfg() -> dict[str, Any]:
    out: dict[str, Any] = {
        "repo_root": "/workspace/skill_system_framework",
        "delivery_target_branch": "main",
        "git_timeout_seconds": 90,
        "update_operational_docs": True,
    }
    try:
        from hermes_cli.config import load_config
        raw = load_config().get("local_main_delivery", {}) or {}
        if isinstance(raw, dict):
            out.update({k: raw[k] for k in out.keys() if k in raw})
    except Exception:
        pass
    return out


def _audit(payload: dict[str, Any]) -> None:
    try:
        path = Path("/opt/data/logs/local-main-delivery/events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": int(time.time()), "profile": _profile(), "plugin_version": VERSION, **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _run(cmd: list[str], *, cwd: Optional[Path] = None, timeout: int = 90) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=max(1, int(timeout)),
        check=False,
    )


def _git(repo: Path, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(repo), *args], timeout=timeout)


def _head(repo: Path, timeout: int) -> str:
    p = _git(repo, ["rev-parse", "--verify", "HEAD^{commit}"], timeout)
    return p.stdout.strip() if p.returncode == 0 else ""


def _branch(repo: Path, timeout: int) -> str:
    p = _git(repo, ["branch", "--show-current"], timeout)
    return p.stdout.strip() if p.returncode == 0 else ""


def _clean(repo: Path, timeout: int) -> tuple[bool, str]:
    p = _git(repo, ["status", "--porcelain=v1", "--untracked-files=all"], timeout)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "")[-1200:]
    return not bool(p.stdout.strip()), p.stdout.strip()


def _normalize_ref(value: Any) -> str:
    ref = str(value or "").strip()
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref


def _extract_checkpoint_yaml(body: str) -> Optional[dict[str, Any]]:
    text = str(body or "").strip()
    if "operational_checkpoint" not in text.lower():
        return None
    lines = text.splitlines()
    if lines and lines[0].startswith("OPERATIONAL_CHECKPOINT_CANONICAL"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        text = "\n".join(lines)
    try:
        parsed = yaml.safe_load(text)
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    if set(parsed.keys()) == {"operational_checkpoint"} and isinstance(parsed.get("operational_checkpoint"), dict):
        return dict(parsed["operational_checkpoint"])
    return parsed


def _latest_checkpoint(conn: Any, task_id: str) -> tuple[Optional[dict[str, Any]], Optional[int]]:
    rows = conn.execute(
        "SELECT id, body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 160",
        (task_id,),
    ).fetchall()
    for row in rows:
        parsed = _extract_checkpoint_yaml(str(row["body"] or ""))
        if parsed is None:
            continue
        nested = parsed.get("operational_checkpoint") if isinstance(parsed.get("operational_checkpoint"), dict) else {}
        phase_id = str(parsed.get("phase_id") or nested.get("phase_id") or "").strip()
        branch = _normalize_ref(
            parsed.get("root_integration_branch")
            or nested.get("root_integration_branch")
            or parsed.get("integration_target_branch")
            or nested.get("integration_target_branch")
        )
        head = str(
            parsed.get("docs_head") or nested.get("docs_head")
            or parsed.get("integration_head") or nested.get("integration_head")
            or parsed.get("code_head") or nested.get("code_head") or ""
        ).strip()
        if phase_id and branch and head:
            parsed["_phase_id"] = phase_id
            parsed["_root_branch"] = branch
            parsed["_expected_head"] = head
            parsed["_nested"] = nested
            return parsed, int(row["id"])
    return None, None


def _completion_gate(cp: dict[str, Any]) -> dict[str, Any]:
    nested = cp.get("_nested") if isinstance(cp.get("_nested"), dict) else {}
    gate = cp.get("completion_gate")
    if not isinstance(gate, dict):
        gate = nested.get("completion_gate") if isinstance(nested.get("completion_gate"), dict) else {}
    return dict(gate)


def _validate_pre_delivery(cp: dict[str, Any], workspace: Path, timeout: int) -> list[str]:
    errors: list[str] = []
    gate = _completion_gate(cp)
    for key in (
        "all_required_cards_integrated",
        "no_open_design_blockers",
        "consolidated_validation_passed",
        "documentation_synchronized",
        "operational_state_updated",
        "branch_clean",
    ):
        if gate.get(key) is not True:
            errors.append(f"completion_gate.{key} must be true before local delivery")
    if gate.get("push_performed") not in (False, None):
        errors.append("completion_gate.push_performed must remain false")
    pending = cp.get("pending_cards")
    if pending is None and isinstance(cp.get("_nested"), dict):
        pending = cp["_nested"].get("pending_cards")
    if pending:
        errors.append(f"pending_cards must be empty before local delivery: {pending}")
    expected = str(cp.get("_expected_head") or "")
    actual = _head(workspace, timeout)
    if not expected or expected != actual:
        errors.append(f"checkpoint head != root workspace HEAD ({expected} != {actual})")
    return errors


def _worktree_for_branch(repo_root: Path, branch: str, timeout: int) -> Optional[Path]:
    p = _git(repo_root, ["worktree", "list", "--porcelain"], timeout)
    if p.returncode != 0:
        return None
    current_path: Optional[Path] = None
    current_branch = ""
    for line in p.stdout.splitlines() + [""]:
        if line.startswith("worktree "):
            current_path = Path(line[len("worktree "):].strip())
            current_branch = ""
        elif line.startswith("branch "):
            current_branch = _normalize_ref(line[len("branch "):].strip())
        elif line.strip() == "":
            if current_path is not None and current_branch == branch:
                return current_path
            current_path = None
            current_branch = ""
    return None


def _deliver_ff_only(root_workspace: Path, main_worktree: Path, target_branch: str, timeout: int) -> dict[str, Any]:
    root_clean, root_status = _clean(root_workspace, timeout)
    main_clean, main_status = _clean(main_worktree, timeout)
    if not root_clean:
        raise RuntimeError(f"root worktree is dirty: {root_status}")
    if not main_clean:
        raise RuntimeError(f"{target_branch} worktree is dirty: {main_status}")
    if _branch(main_worktree, timeout) != target_branch:
        raise RuntimeError(f"delivery worktree is not on {target_branch}")

    source_head = _head(root_workspace, timeout)
    main_before = _head(main_worktree, timeout)
    if not source_head or not main_before:
        raise RuntimeError("cannot resolve delivery heads")

    anc = _git(main_worktree, ["merge-base", "--is-ancestor", main_before, source_head], timeout)
    if anc.returncode != 0:
        raise RuntimeError(
            "local main diverged from validated root head; reconciliation + impacted revalidation required"
        )

    if main_before != source_head:
        merge = _git(main_worktree, ["merge", "--ff-only", source_head], timeout)
        if merge.returncode != 0:
            raise RuntimeError(f"ff-only delivery failed: {(merge.stderr or merge.stdout)[-1600:]}")

    main_after = _head(main_worktree, timeout)
    if main_after != source_head:
        raise RuntimeError(f"post-delivery main HEAD mismatch: {main_after} != {source_head}")
    clean_after, status_after = _clean(main_worktree, timeout)
    if not clean_after:
        raise RuntimeError(f"main became dirty after ff-only delivery: {status_after}")
    return {"source_head": source_head, "main_before": main_before, "main_after": main_after}


def _managed_block(text: str, marker: str, body: str, prefix: str = "<!--", suffix: str = "-->") -> str:
    if prefix == "#":
        begin = f"# {marker}:BEGIN"
        end = f"# {marker}:END"
    else:
        begin = f"<!-- {marker}:BEGIN -->"
        end = f"<!-- {marker}:END -->"
    block = f"{begin}\n{body.rstrip()}\n{end}"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    return text.rstrip() + "\n\n" + block + "\n"


def _post_delivery_docs(main_worktree: Path, *, phase_id: str, root_task_id: str, root_branch: str,
                        target_branch: str, delivered_head: str, timeout: int) -> Optional[str]:
    estado = main_worktree / "ai_system_docs/00-visao-geral/estado-atual-ai-system.md"
    manifest = main_worktree / "ai_system_docs/05-orquestracao/implementation-manifest.yaml"
    changed: list[Path] = []

    if estado.is_file():
        text = estado.read_text(encoding="utf-8")
        body = "\n".join([
            "## Entrega local canonica - estado mais recente",
            "",
            "- development_mode: `POLISH_DEBUG`",
            "- terminal_phase: `FASE 15`",
            f"- phase_id: `{phase_id}`",
            f"- root_task_id: `{root_task_id}`",
            f"- root_integration_branch: `{root_branch}`",
            f"- delivery_target_branch: `{target_branch}`",
            f"- validated_delivery_head: `{delivered_head}`",
            "- main_integration_pending: `false`",
            "- main_integrated: `true`",
            "- push_performed: `false`",
        ])
        new = _managed_block(text, "HERMES_CANONICAL_DELIVERY_LATEST", body)
        if new != text:
            estado.write_text(new, encoding="utf-8")
            changed.append(estado)

    if manifest.is_file():
        text = manifest.read_text(encoding="utf-8")
        body = "\n".join([
            "operational_delivery_latest:",
            "  version: 1",
            "  development_mode: POLISH_DEBUG",
            "  terminal_phase: FASE 15",
            f"  phase_id: {phase_id}",
            f"  root_task_id: {root_task_id}",
            f"  root_integration_branch: {root_branch}",
            f"  delivery_target_branch: {target_branch}",
            f"  validated_delivery_head: {delivered_head}",
            "  main_integration_pending: false",
            "  main_integrated: true",
            "  push_performed: false",
        ])
        new = _managed_block(text, "HERMES_CANONICAL_DELIVERY_LATEST", body, prefix="#", suffix="")
        if new != text:
            manifest.write_text(new, encoding="utf-8")
            changed.append(manifest)

    if not changed:
        return None

    rels = [str(p.relative_to(main_worktree)).replace("\\", "/") for p in changed]
    add = _git(main_worktree, ["add", "--", *rels], timeout)
    if add.returncode != 0:
        raise RuntimeError(f"git add post-delivery docs failed: {(add.stderr or add.stdout)[-1200:]}")
    diff = _git(main_worktree, ["diff", "--cached", "--quiet"], timeout)
    if diff.returncode == 0:
        return None
    if diff.returncode != 1:
        raise RuntimeError("cannot inspect staged post-delivery docs")
    commit = _git(main_worktree, ["commit", "-m", f"docs(operational): record local main delivery {phase_id}"], timeout)
    if commit.returncode != 0:
        raise RuntimeError(f"post-delivery docs commit failed: {(commit.stderr or commit.stdout)[-1600:]}")
    return _head(main_worktree, timeout)


def _publish_checkpoint(conn: Any, *, root_id: str, cp: dict[str, Any], root_branch: str,
                        target_branch: str, source_head: str, final_head: str, main_before: str) -> None:
    from hermes_cli import kanban_db as kb
    nested = cp.get("_nested") if isinstance(cp.get("_nested"), dict) else {}
    phase_id = str(cp.get("_phase_id") or "")
    phase_base = str(cp.get("phase_base_commit") or nested.get("phase_base_commit") or "")
    architecture_revision = cp.get("architecture_revision", nested.get("architecture_revision"))
    code_head = str(cp.get("code_head") or nested.get("code_head") or source_head)
    integrated_cards = cp.get("integrated_cards", nested.get("integrated_cards", [])) or []
    validated_sources = cp.get("validated_sources", nested.get("validated_sources", [])) or []

    payload = {
        "operational_checkpoint": {
            "version": 3,
            "phase_id": phase_id,
            "status": "DELIVERED_LOCAL",
            "root_integration_branch": root_branch,
            "delivery_target_branch": target_branch,
            "phase_base_commit": phase_base,
            "integration_head": final_head,
            "code_head": code_head,
            "docs_head": final_head,
            "architecture_revision": architecture_revision,
            "integrated_cards": integrated_cards,
            "pending_cards": [],
            "next_expected_handoffs": [],
            "validated_sources": validated_sources,
            "completion_gate": {
                "all_required_cards_integrated": True,
                "no_open_design_blockers": True,
                "consolidated_validation_passed": True,
                "documentation_synchronized": True,
                "operational_state_updated": True,
                "branch_clean": True,
                "delivery_to_main_completed": True,
                "push_performed": False,
            },
            "delivery_state": {
                "integration_branch_completed": True,
                "integration_source_branch": root_branch,
                "integration_source_head": source_head,
                "delivery_target_branch": target_branch,
                "main_head_before": main_before,
                "main_head_observed": final_head,
                "main_integration_pending": False,
                "main_integrated": True,
                "push_performed": False,
            },
        }
    }
    body = "OPERATIONAL_CHECKPOINT_CANONICAL v3\n" + yaml.safe_dump(
        payload, sort_keys=False, allow_unicode=True
    )
    kb.add_comment(conn, root_id, author="local-main-delivery", body=body)


def _finalize_handler(args: dict[str, Any], **_: Any) -> str:
    if _profile() not in ALLOWED_PROFILES:
        return _err("local main delivery is orchestrator-only")
    root_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    workspace_raw = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    board = str(os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None
    if not root_id or not workspace_raw:
        return _err("missing HERMES_KANBAN_TASK/HERMES_KANBAN_WORKSPACE")

    cfg = _cfg()
    timeout = int(cfg.get("git_timeout_seconds") or 90)
    workspace = Path(workspace_raw).resolve()
    repo_root = Path(str(cfg.get("repo_root") or "/workspace/skill_system_framework")).resolve()
    target_branch = _normalize_ref(cfg.get("delivery_target_branch") or "main")
    event = {"event": "finalize", "root_task_id": root_id, "workspace": str(workspace)}

    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc
        with kbc.connect_closing(board=board) as conn:
            root = kb.get_task(conn, root_id)
            if not root:
                return _err("root task not found")
            cp, cp_comment_id = _latest_checkpoint(conn, root_id)
            if cp is None:
                return _err("canonical operational checkpoint not found; run operational_sync_finalize first")

            root_branch = _normalize_ref(cp.get("_root_branch"))
            current_branch = _branch(workspace, timeout)
            if current_branch != root_branch:
                return _err("current root workspace branch does not match canonical root branch",
                            current_branch=current_branch, root_branch=root_branch)
            errors = _validate_pre_delivery(cp, workspace, timeout)
            if errors:
                return _err("pre-delivery gate failed", errors=errors, checkpoint_comment_id=cp_comment_id)

            main_worktree = _worktree_for_branch(repo_root, target_branch, timeout)
            if main_worktree is None:
                return _err(f"no local worktree found for delivery target branch {target_branch}")

            delivery = _deliver_ff_only(workspace, main_worktree, target_branch, timeout)
            source_head = str(delivery["source_head"])
            final_head = source_head

            if bool(cfg.get("update_operational_docs", True)):
                post = _post_delivery_docs(
                    main_worktree,
                    phase_id=str(cp.get("_phase_id") or ""),
                    root_task_id=root_id,
                    root_branch=root_branch,
                    target_branch=target_branch,
                    delivered_head=source_head,
                    timeout=timeout,
                )
                if post:
                    final_head = post

            # Keep the root integration branch and local main on the same final local head.
            if _head(workspace, timeout) != final_head:
                ff_back = _git(workspace, ["merge", "--ff-only", final_head], timeout)
                if ff_back.returncode != 0:
                    raise RuntimeError(f"cannot fast-forward root branch to post-delivery head: {(ff_back.stderr or ff_back.stdout)[-1600:]}")

            if _head(main_worktree, timeout) != final_head or _head(workspace, timeout) != final_head:
                raise RuntimeError("root/main final heads differ after post-delivery sync")
            root_clean, root_status = _clean(workspace, timeout)
            main_clean, main_status = _clean(main_worktree, timeout)
            if not root_clean or not main_clean:
                raise RuntimeError(f"post-delivery worktree dirty: root={root_status!r} main={main_status!r}")

            _publish_checkpoint(
                conn,
                root_id=root_id,
                cp=cp,
                root_branch=root_branch,
                target_branch=target_branch,
                source_head=source_head,
                final_head=final_head,
                main_before=str(delivery["main_before"]),
            )
            _audit({**event, "result": "ok", "source_head": source_head, "final_head": final_head,
                    "main_worktree": str(main_worktree), "push_performed": False})
            return _ok(
                root_task_id=root_id,
                root_integration_branch=root_branch,
                delivery_target_branch=target_branch,
                source_head=source_head,
                final_head=final_head,
                main_worktree=str(main_worktree),
                main_integration_pending=False,
                main_integrated=True,
                push_performed=False,
            )
    except Exception as exc:
        _audit({**event, "result": "error", "error": f"{type(exc).__name__}: {exc}"[:2000]})
        return _err(f"{type(exc).__name__}: {exc}"[:2000], human_required=True)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="local_main_delivery_finalize",
        toolset=TOOLSET,
        schema=FINALIZE_SCHEMA,
        handler=_finalize_handler,
        check_fn=_available,
    )

# HERMES_LOCAL_DELIVERY_RECOVERY_V2_2026_09_14
_ORIGINAL_LOCAL_MAIN_DELIVERY_FINALIZE_V2 = _finalize_handler


def _delivery_checkpoint_yaml_payload_v2(body: str) -> str:
    text = str(body or "").strip()
    lines = text.splitlines()
    if lines and lines[0].startswith("OPERATIONAL_CHECKPOINT_CANONICAL"):
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        text = "\n".join(lines).strip()

    fenced = re.search(
        r"```(?:yaml|yml)?\s*\r?\n(.*?)\r?\n```",
        text,
        re.IGNORECASE | re.DOTALL,
    )
    return fenced.group(1).strip() if fenced else text


def _extract_checkpoint_yaml(body: str) -> Optional[dict[str, Any]]:
    text = str(body or "").strip()
    if "operational_checkpoint" not in text.lower():
        return None
    try:
        parsed = yaml.safe_load(
            _delivery_checkpoint_yaml_payload_v2(text)
        )
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _delivery_state_v2(cp: dict[str, Any]) -> dict[str, Any]:
    nested = cp.get("_nested")
    nested = nested if isinstance(nested, dict) else {}
    state = cp.get("delivery_state")
    if not isinstance(state, dict):
        state = nested.get("delivery_state")
    return dict(state) if isinstance(state, dict) else {}


def _delivery_idempotent_v2(
    cp: dict[str, Any],
    root_workspace: Path,
    main_worktree: Path,
    target_branch: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    delivery = _delivery_state_v2(cp)
    nested = (
        cp.get("_nested")
        if isinstance(cp.get("_nested"), dict)
        else {}
    )
    target = str(
        cp.get("delivery_target_branch")
        or nested.get("delivery_target_branch")
        or delivery.get("delivery_target_branch")
        or ""
    ).strip()

    if not (
        target == target_branch
        and delivery.get("main_integrated") is True
        and delivery.get("main_integration_pending") is False
        and delivery.get("push_performed") in (False, None)
    ):
        return None

    expected = str(cp.get("_expected_head") or "")
    root_head = _head(root_workspace, timeout)
    main_head = _head(main_worktree, timeout)
    root_clean, _ = _clean(root_workspace, timeout)
    main_clean, _ = _clean(main_worktree, timeout)

    if (
        expected
        and root_head == expected
        and main_head == expected
        and root_clean
        and main_clean
    ):
        return {
            "idempotent": True,
            "final_head": expected,
            "source_head": str(
                delivery.get("integration_source_head")
                or expected
            ),
        }
    return None


def _recover_partial_delivery_v2(
    conn: Any,
    *,
    root_id: str,
    cp: dict[str, Any],
    root_branch: str,
    root_workspace: Path,
    main_worktree: Path,
    target_branch: str,
    timeout: int,
) -> Optional[dict[str, Any]]:
    expected_source = str(
        cp.get("_expected_head") or ""
    ).strip()
    if not expected_source:
        return None

    root_clean, _ = _clean(root_workspace, timeout)
    main_clean, _ = _clean(main_worktree, timeout)
    if not root_clean or not main_clean:
        return None

    root_head = _head(root_workspace, timeout)
    main_head = _head(main_worktree, timeout)
    if not main_head or main_head == expected_source:
        return None

    parent = _git(
        main_worktree,
        ["rev-parse", "--verify", f"{main_head}^"],
        timeout,
    )
    if (
        parent.returncode != 0
        or parent.stdout.strip() != expected_source
    ):
        return None

    subject = _git(
        main_worktree,
        ["show", "-s", "--format=%s", main_head],
        timeout,
    )
    phase_id = str(cp.get("_phase_id") or "")
    expected_subject = (
        f"docs(operational): record local main delivery {phase_id}"
    )
    if (
        subject.returncode != 0
        or subject.stdout.strip() != expected_subject
    ):
        return None

    changed = _git(
        main_worktree,
        [
            "diff",
            "--name-only",
            f"{expected_source}..{main_head}",
            "--",
        ],
        timeout,
    )
    changed_paths = {
        line.strip()
        for line in changed.stdout.splitlines()
        if line.strip()
    } if changed.returncode == 0 else set()

    allowed = {
        "ai_system_docs/00-visao-geral/estado-atual-ai-system.md",
        "ai_system_docs/05-orquestracao/implementation-manifest.yaml",
    }
    if not changed_paths or not changed_paths.issubset(allowed):
        return None

    for rel in changed_paths:
        text = (
            main_worktree / rel
        ).read_text(encoding="utf-8")
        if (
            "HERMES_CANONICAL_DELIVERY_LATEST" not in text
            or root_id not in text
            or target_branch not in text
        ):
            return None

    if root_head == expected_source:
        ff = _git(
            root_workspace,
            ["merge", "--ff-only", main_head],
            timeout,
        )
        if ff.returncode != 0:
            raise RuntimeError(
                "cannot recover exact partial delivery by ff root: "
                f"{(ff.stderr or ff.stdout)[-1200:]}"
            )
        root_head = _head(root_workspace, timeout)

    if root_head != main_head:
        return None

    _publish_checkpoint(
        conn,
        root_id=root_id,
        cp=cp,
        root_branch=root_branch,
        target_branch=target_branch,
        source_head=expected_source,
        final_head=main_head,
        main_before=expected_source,
    )
    return {
        "recovered_partial_success": True,
        "source_head": expected_source,
        "final_head": main_head,
    }


def _finalize_handler(
    args: dict[str, Any],
    **kwargs: Any,
) -> str:
    if _profile() not in ALLOWED_PROFILES:
        return _ORIGINAL_LOCAL_MAIN_DELIVERY_FINALIZE_V2(
            args,
            **kwargs,
        )

    root_id = str(
        os.environ.get("HERMES_KANBAN_TASK") or ""
    ).strip()
    workspace_raw = str(
        os.environ.get("HERMES_KANBAN_WORKSPACE") or ""
    ).strip()
    board = str(
        os.environ.get("HERMES_KANBAN_BOARD") or ""
    ).strip() or None

    if not root_id or not workspace_raw:
        return _ORIGINAL_LOCAL_MAIN_DELIVERY_FINALIZE_V2(
            args,
            **kwargs,
        )

    cfg = _cfg()
    timeout = int(cfg.get("git_timeout_seconds") or 90)
    workspace = Path(workspace_raw).resolve()
    repo_root = Path(
        str(
            cfg.get("repo_root")
            or "/workspace/skill_system_framework"
        )
    ).resolve()
    target_branch = _normalize_ref(
        cfg.get("delivery_target_branch") or "main"
    )

    try:
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing(board=board) as conn:
            cp, _cp_comment_id = _latest_checkpoint(
                conn,
                root_id,
            )
            if cp is None:
                return _ORIGINAL_LOCAL_MAIN_DELIVERY_FINALIZE_V2(
                    args,
                    **kwargs,
                )

            root_branch = _normalize_ref(
                cp.get("_root_branch")
            )
            main_worktree = _worktree_for_branch(
                repo_root,
                target_branch,
                timeout,
            )
            if main_worktree is None:
                return _err(
                    "no local worktree found for delivery target "
                    f"branch {target_branch}"
                )

            idem = _delivery_idempotent_v2(
                cp,
                workspace,
                main_worktree,
                target_branch,
                timeout,
            )
            if idem is not None:
                _audit({
                    "event": "finalize",
                    "root_task_id": root_id,
                    "result": "idempotent",
                    **idem,
                })
                return _ok(
                    root_task_id=root_id,
                    root_integration_branch=root_branch,
                    delivery_target_branch=target_branch,
                    main_integration_pending=False,
                    main_integrated=True,
                    push_performed=False,
                    **idem,
                )

            recovered = _recover_partial_delivery_v2(
                conn,
                root_id=root_id,
                cp=cp,
                root_branch=root_branch,
                root_workspace=workspace,
                main_worktree=main_worktree,
                target_branch=target_branch,
                timeout=timeout,
            )
            if recovered is not None:
                _audit({
                    "event": "finalize",
                    "root_task_id": root_id,
                    "result": "recovered_partial_success",
                    **recovered,
                })
                return _ok(
                    root_task_id=root_id,
                    root_integration_branch=root_branch,
                    delivery_target_branch=target_branch,
                    main_integration_pending=False,
                    main_integrated=True,
                    push_performed=False,
                    **recovered,
                )
    except Exception as exc:
        _audit({
            "event": "finalize_preflight_v2_error",
            "root_task_id": root_id,
            "error": f"{type(exc).__name__}: {exc}"[:2000],
        })
        return _err(
            f"{type(exc).__name__}: {exc}"[:2000],
            human_required=True,
        )

    return _ORIGINAL_LOCAL_MAIN_DELIVERY_FINALIZE_V2(
        args,
        **kwargs,
    )
