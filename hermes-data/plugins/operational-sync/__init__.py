"""Deterministic operational docs/manifest/Graphify synchronization for Hermes.

The plugin replaces the former DOCS-SYNC worker card with one orchestrator-only
operation.  The LLM supplies only already-known semantic facts; Git/Kanban
identity, heads, branch cleanliness, artifact rendering, Graphify, commit and
checkpoint publication are deterministic and fail closed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Optional

import yaml

NAME = "operational-sync"
VERSION = "1.0.0"
TOOLSET = "operational_sync"
ALLOWED_PROFILES = {"implementation-orchestrator"}
MARKER = "HERMES_OPERATIONAL_SYNC_2026_09_07"

FINALIZE_SCHEMA = {
    "name": "operational_sync_finalize",
    "description": (
        "Synchronize operational evidence/docs/manifest and incremental Graphify directly in the "
        "current orchestrator integration worktree after the consolidated gate is green. "
        "No worker/card, GUT run, architecture edit, roadmap promotion or push is performed."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "gate_task_id": {
                "type": "string",
                "description": "Completed consolidated validation/gate card already integrated/recorded in the checkpoint.",
            },
            "summary_facts": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 12,
                "description": "Short already-verified semantic facts for the human operational view.",
            },
            "validation_evidence": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {
                    "type": "object",
                    "properties": {
                        "suite": {"type": "string"},
                        "status": {"type": "string"},
                        "operation_id": {"type": "string"},
                        "scripts": {"type": "integer"},
                        "tests": {"type": "integer"},
                        "asserts": {"type": "integer"},
                        "note": {"type": "string"},
                    },
                    "required": ["suite", "status"],
                    "additionalProperties": False,
                },
                "description": "Structured PASS evidence already known from the consolidated gate.",
            },
            "architecture_decisions": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "properties": {
                        "revision": {"type": "string"},
                        "label": {"type": "string"},
                        "summary": {"type": "string"},
                    },
                    "required": ["summary"],
                    "additionalProperties": False,
                },
                "description": "Short approved architecture decision summaries; may be empty.",
            },
            "risks": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 12,
                "description": "Persistent verified risks/pending human gates; may be empty.",
            },
            "graphify_mode": {
                "type": "string",
                "enum": ["auto", "force"],
                "description": "auto runs Graphify only when phase_base..code_head contains non-operational-sync changes; force always runs it.",
            },
            "reason": {"type": "string", "description": "Short audit reason."},
        },
        "required": ["gate_task_id", "summary_facts", "validation_evidence"],
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
    defaults: dict[str, Any] = {
        "workspace_root": "/workspace",
        "repo_root": "/workspace/skill_system_framework",
        "git_timeout_seconds": 60,
        "graphify_timeout_seconds": 300,
        "graphify_enabled": True,
        "docs": {
            "estado_atual": "ai_system_docs/00-visao-geral/estado-atual-ai-system.md",
            "indice": "ai_system_docs/00-visao-geral/indice-documentacao.md",
            "manifest_md": "ai_system_docs/MANIFEST.md",
            "implementation_manifest": "ai_system_docs/05-orquestracao/implementation-manifest.yaml",
            "evidence_dir": "ai_system_docs/05-orquestracao",
        },
    }
    try:
        from hermes_cli.config import load_config

        raw = load_config().get("operational_sync", {}) or {}
        if isinstance(raw, dict):
            for key in (
                "workspace_root",
                "repo_root",
                "git_timeout_seconds",
                "graphify_timeout_seconds",
                "graphify_enabled",
            ):
                if key in raw:
                    defaults[key] = raw[key]
            incoming_docs = raw.get("docs")
            if isinstance(incoming_docs, dict):
                defaults["docs"].update(incoming_docs)
    except Exception:
        pass
    return defaults


def _audit(payload: dict[str, Any]) -> None:
    try:
        path = Path("/opt/data/logs/operational-sync/events.jsonl")
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts": int(time.time()), "profile": _profile(), "plugin_version": VERSION, **payload}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except Exception:
        pass


def _run(cmd: list[str], *, cwd: Optional[Path] = None, timeout: int = 60) -> subprocess.CompletedProcess:
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


def _git(workspace: Path, args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return _run(["git", "-C", str(workspace), *args], timeout=timeout)


def _resolve_commit(workspace: Path, ref: str, timeout: int) -> Optional[str]:
    if not ref:
        return None
    p = _git(workspace, ["rev-parse", "--verify", f"{ref}^{{commit}}"], timeout)
    return p.stdout.strip() if p.returncode == 0 else None


def _git_clean(workspace: Path, timeout: int) -> tuple[bool, str]:
    p = _git(workspace, ["status", "--porcelain=v1", "--untracked-files=all"], timeout)
    if p.returncode != 0:
        return False, (p.stderr or p.stdout or "")[-1000:]
    return not bool(p.stdout.strip()), p.stdout.strip()


def _current_branch(workspace: Path, timeout: int) -> Optional[str]:
    p = _git(workspace, ["branch", "--show-current"], timeout)
    return p.stdout.strip() if p.returncode == 0 and p.stdout.strip() else None


def _head(workspace: Path, timeout: int) -> Optional[str]:
    return _resolve_commit(workspace, "HEAD", timeout)


def _safe_one_line(value: Any, limit: int = 1200) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _slug(phase_id: str) -> str:
    value = phase_id.strip().lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9.-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-.")
    if not value:
        raise ValueError("phase_id cannot be converted to safe evidence slug")
    return value


def _normalize_ref(value: Any) -> str:
    ref = str(value or "").strip()
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


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
    elif lines and lines[0].lower().startswith("operational_checkpoint (canonical"):
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


def _normalize_checkpoint(parsed: dict[str, Any]) -> dict[str, Any]:
    nested = parsed.get("operational_checkpoint")
    nested = nested if isinstance(nested, dict) else {}
    cp: dict[str, Any] = {}
    for key in (
        "phase_id",
        "status",
        "integration_target_branch",
        "phase_base_commit",
        "integration_head",
        "architecture_revision",
        "code_head",
        "docs_head",
        "completion_gate",
        "integrated_cards",
        "pending_cards",
        "next_expected_handoffs",
        "validated_sources",
    ):
        if key in parsed:
            cp[key] = parsed[key]
        elif key in nested:
            cp[key] = nested[key]
    cp["version"] = nested.get("version", parsed.get("version", 1))
    cp["raw"] = parsed
    return cp


def _latest_checkpoint(conn: Any, task_id: str) -> tuple[Optional[dict[str, Any]], Optional[int], Optional[str]]:
    rows = conn.execute(
        "SELECT id, author, body FROM task_comments WHERE task_id = ? ORDER BY id DESC LIMIT 120",
        (task_id,),
    ).fetchall()
    for row in rows:
        body = str(row["body"] or "")
        parsed = _extract_checkpoint_yaml(body)
        if parsed is None:
            continue
        cp = _normalize_checkpoint(parsed)
        if cp.get("phase_id") and cp.get("integration_target_branch") and cp.get("integration_head"):
            return cp, int(row["id"]), body
    return None, None, None


def _validate_checkpoint(cp: dict[str, Any], gate_task_id: str) -> list[str]:
    errors: list[str] = []
    phase_id = str(cp.get("phase_id") or "").strip()
    branch = _normalize_ref(cp.get("integration_target_branch"))
    base = str(cp.get("phase_base_commit") or "").strip()
    head = str(cp.get("integration_head") or cp.get("code_head") or "").strip()
    if not phase_id:
        errors.append("checkpoint missing phase_id")
    if not branch:
        errors.append("checkpoint missing integration_target_branch")
    if not base:
        errors.append("checkpoint missing phase_base_commit")
    if not head:
        errors.append("checkpoint missing integration_head")

    gate = cp.get("completion_gate") or {}
    if not isinstance(gate, dict):
        errors.append("checkpoint completion_gate is not a mapping")
        gate = {}
    for key in ("all_required_cards_integrated", "no_open_design_blockers", "consolidated_validation_passed", "branch_clean"):
        if gate.get(key) is not True:
            errors.append(f"completion_gate.{key} must be true before operational sync")
    if gate.get("push_performed") not in (False, None):
        errors.append("completion_gate.push_performed must remain false")
    if gate.get("documentation_synchronized") is True and gate.get("operational_state_updated") is True:
        # Idempotence is handled later from docs_head/current HEAD, not an error.
        pass

    pending = cp.get("pending_cards") or []
    if pending:
        errors.append(f"checkpoint pending_cards must be empty before operational sync: {pending}")
    integrated = {str(x).split()[0] for x in (cp.get("integrated_cards") or [])}
    if gate_task_id not in integrated:
        errors.append("gate_task_id is not present in checkpoint integrated_cards")
    return errors


def _managed_block(text: str, marker: str, rendered: str, *, insert_after_title: bool = True) -> str:
    begin = f"<!-- {marker}:BEGIN -->"
    end = f"<!-- {marker}:END -->"
    block = f"{begin}\n{rendered.rstrip()}\n{end}"
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    if insert_after_title:
        lines = text.splitlines()
        if lines and lines[0].startswith("#"):
            return "\n".join([lines[0], "", block, "", *lines[1:]]).rstrip() + "\n"
    return text.rstrip() + "\n\n" + block + "\n"


def _update_unique_yaml_scalar(text: str, key: str, value: str) -> str:
    pattern = re.compile(rf"(?m)^(\s{{2}}{re.escape(key)}:\s*).*$")
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise RuntimeError(f"implementation-manifest expected exactly one current_state {key}, found {len(matches)}")
    safe = _safe_one_line(value, 4000)
    return pattern.sub(lambda m: m.group(1) + safe, text, count=1)


def _render_validation_table(items: list[dict[str, Any]]) -> str:
    rows = [
        "| Suite | Status | Operation | Scripts | Tests | Asserts | Nota |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for item in items:
        def cell(name: str) -> str:
            v = item.get(name)
            if v is None:
                return "—"
            return _safe_one_line(v, 300).replace("|", "\\|")
        rows.append(
            f"| {cell('suite')} | {cell('status')} | {cell('operation_id')} | "
            f"{cell('scripts')} | {cell('tests')} | {cell('asserts')} | {cell('note')} |"
        )
    return "\n".join(rows)


def _render_evidence(
    *,
    phase_id: str,
    root_task_id: str,
    gate_task_id: str,
    branch: str,
    phase_base: str,
    code_head: str,
    architecture_revision: Optional[str],
    summary_facts: list[str],
    validation_evidence: list[dict[str, Any]],
    architecture_decisions: list[dict[str, Any]],
    risks: list[str],
) -> str:
    lines = [
        f"# Evidência operacional — {phase_id}",
        "",
        f"> **Gerado automaticamente por `{NAME} {VERSION}`.** Não substitui contratos arquiteturais; registra o fechamento verificado do recorte.",
        "",
        "## Identidade",
        "",
        f"- root card: `{root_task_id}`",
        f"- gate consolidado: `{gate_task_id}`",
        f"- integration branch: `{branch}`",
        f"- phase base: `{phase_base}`",
        f"- code head validado: `{code_head}`",
        f"- architecture revision: `{architecture_revision or 'null'}`",
        "- push_performed: `false`",
        "",
        "## Resumo verificado",
        "",
    ]
    lines += [f"- {_safe_one_line(x)}" for x in summary_facts]
    lines += ["", "## Validação consolidada", "", _render_validation_table(validation_evidence)]
    lines += ["", "## Decisões arquiteturais consumidas", ""]
    if architecture_decisions:
        for item in architecture_decisions:
            label = _safe_one_line(item.get("label") or "decision")
            rev = _safe_one_line(item.get("revision") or architecture_revision or "null")
            summary = _safe_one_line(item.get("summary"))
            lines.append(f"- **{label}** (`{rev}`): {summary}")
    else:
        lines.append(f"- Nenhuma nova decisão declarada no sync; revisão consumida `{architecture_revision or 'null'}`.")
    lines += ["", "## Riscos / pendências preservadas", ""]
    if risks:
        lines += [f"- {_safe_one_line(x)}" for x in risks]
    else:
        lines.append("- Nenhum risco adicional informado para este fechamento.")
    lines += [
        "",
        "## Regras preservadas",
        "",
        "- Este sync não executa GUT nem altera código/contratos/roadmap.",
        "- Graphify, quando necessário, é incremental e executado uma única vez no estado final.",
        "- Nenhum agente executa push.",
        "",
    ]
    return "\n".join(lines)


def _render_estado_block(phase_id: str, root_task_id: str, gate_task_id: str, branch: str, code_head: str, architecture_revision: Optional[str], evidence_rel: str, summary_facts: list[str], risks: list[str]) -> str:
    lines = [
        "## Estado operacional mais recente — gerado automaticamente",
        "",
        "> Este bloco é a visão operacional mais recente gerada deterministicamente. O conteúdo histórico abaixo é preservado para auditoria.",
        "",
        f"- **Recorte:** `{phase_id}`",
        f"- **Root:** `{root_task_id}`",
        f"- **Gate consolidado:** `{gate_task_id}`",
        f"- **Branch de integração:** `{branch}`",
        f"- **Code head validado:** `{code_head}`",
        f"- **Architecture revision:** `{architecture_revision or 'null'}`",
        f"- **Evidência:** [`{Path(evidence_rel).name}`](../05-orquestracao/{Path(evidence_rel).name})",
        "- **Push:** `false`",
        "",
        "### Resumo",
        "",
    ]
    lines += [f"- {_safe_one_line(x)}" for x in summary_facts]
    lines += ["", "### Riscos / pendências", ""]
    lines += [f"- {_safe_one_line(x)}" for x in risks] if risks else ["- Nenhum risco adicional informado para este fechamento."]
    return "\n".join(lines)


def _render_index_block(phase_id: str, evidence_rel: str) -> str:
    filename = Path(evidence_rel).name
    return "\n".join(
        [
            "## Leitura operacional mais recente — gerada automaticamente",
            "",
            f"Para auditar `{phase_id}` com contexto mínimo:",
            "",
            f"1. [`{filename}`](../05-orquestracao/{filename})",
            "2. [`../05-orquestracao/implementation-manifest.yaml`](../05-orquestracao/implementation-manifest.yaml)",
            "3. [`estado-atual-ai-system.md`](estado-atual-ai-system.md)",
        ]
    )


def _render_manifest_md_block(phase_id: str, evidence_rel: str, root_task_id: str, gate_task_id: str, code_head: str, architecture_revision: Optional[str]) -> str:
    filename = Path(evidence_rel).name
    authority = "OPERACIONAL_" + re.sub(r"[^A-Z0-9]+", "_", phase_id.upper()).strip("_") + "_GATE"
    return "\n".join(
        [
            "## Catálogo operacional mais recente — gerado automaticamente",
            "",
            "| Caminho | Finalidade | Consumidor | Quando ler | Autoridade |",
            "|---|---|---|---|---|",
            f"| `05-orquestracao/{filename}` | Evidência consolidada de `{phase_id}` (root `{root_task_id}`, gate `{gate_task_id}`, code head `{code_head[:12]}`, arquitetura `{(architecture_revision or 'null')[:12]}`) | ambos | auditar o último recorte verificado | {authority} |",
            "| `05-orquestracao/implementation-manifest.yaml` | Manifesto legível por máquina com `operational_sync_latest` | implementation-orchestrator | fast resume / planejamento | Canônico operacional |",
        ]
    )


def _render_manifest_yaml_block(data: dict[str, Any]) -> str:
    dumped = yaml.safe_dump(
        {"operational_sync_latest": data},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    ).rstrip()
    return "\n".join(
        [
            "# OPERATIONAL_SYNC_MANAGED_BEGIN",
            dumped,
            "# OPERATIONAL_SYNC_MANAGED_END",
        ]
    )


def _replace_manifest_managed_block(text: str, data: dict[str, Any]) -> str:
    begin = "# OPERATIONAL_SYNC_MANAGED_BEGIN"
    end = "# OPERATIONAL_SYNC_MANAGED_END"
    block = _render_manifest_yaml_block(data)
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end), re.S)
    if pattern.search(text):
        return pattern.sub(block, text, count=1)
    return text.rstrip() + "\n\n" + block + "\n"


def _graphify_delta_paths(workspace: Path, phase_base: str, code_head: str, timeout: int) -> list[str]:
    p = _git(workspace, ["diff", "--name-only", f"{phase_base}..{code_head}", "--"], timeout)
    if p.returncode != 0:
        raise RuntimeError(f"cannot determine Graphify delta: {(p.stderr or p.stdout)[-1000:]}")
    return [line.strip() for line in p.stdout.splitlines() if line.strip()]


def _graphify_required(paths: list[str]) -> bool:
    exact_operational = {
        "ai_system_docs/00-visao-geral/estado-atual-ai-system.md",
        "ai_system_docs/00-visao-geral/indice-documentacao.md",
        "ai_system_docs/MANIFEST.md",
        "ai_system_docs/05-orquestracao/implementation-manifest.yaml",
    }
    for path in paths:
        p = path.replace("\\", "/")
        if p.startswith("graphify-out/"):
            continue
        if p in exact_operational:
            continue
        if p.startswith("ai_system_docs/05-orquestracao/") and p.endswith("-gate-evidence.md"):
            continue
        return True
    return False


def _changed_paths(workspace: Path, timeout: int) -> list[str]:
    tracked = _git(workspace, ["diff", "--name-only", "--"], timeout)
    if tracked.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {(tracked.stderr or tracked.stdout)[-1000:]}")
    untracked = _git(workspace, ["ls-files", "--others", "--exclude-standard"], timeout)
    if untracked.returncode != 0:
        raise RuntimeError(f"git ls-files --others failed: {(untracked.stderr or untracked.stdout)[-1000:]}")
    return sorted({x.strip() for x in (tracked.stdout + "\n" + untracked.stdout).splitlines() if x.strip()})


def _allowed_generated_path(path: str, evidence_rel: str, cfg: dict[str, Any]) -> bool:
    docs = cfg["docs"]
    exact = {
        docs["estado_atual"],
        docs["indice"],
        docs["manifest_md"],
        docs["implementation_manifest"],
        evidence_rel,
    }
    p = path.replace("\\", "/")
    return p in exact or p.startswith("graphify-out/")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _synchronize_workspace(
    *,
    workspace: Path,
    root_task_id: str,
    gate_task_id: str,
    checkpoint: dict[str, Any],
    args: dict[str, Any],
    cfg: dict[str, Any],
    graphify_runner: Optional[Callable[[Path, int], subprocess.CompletedProcess]] = None,
    commit: bool = True,
) -> dict[str, Any]:
    timeout = max(1, int(cfg.get("git_timeout_seconds", 60)))
    graphify_timeout = max(1, int(cfg.get("graphify_timeout_seconds", 300)))
    phase_id = str(checkpoint.get("phase_id") or "").strip()
    branch = _normalize_ref(checkpoint.get("integration_target_branch"))
    phase_base = str(checkpoint.get("phase_base_commit") or "").strip()
    code_head = str(checkpoint.get("code_head") or checkpoint.get("integration_head") or "").strip()
    architecture_revision = str(checkpoint.get("architecture_revision") or "").strip() or None

    expected_root_branch = f"wt/{root_task_id}"
    if branch != expected_root_branch:
        raise RuntimeError(f"operational sync requires canonical root branch {expected_root_branch}, got {branch}")
    if workspace.name != root_task_id or workspace.parent.name != ".worktrees":
        raise RuntimeError("operational sync requires canonical <repo>/.worktrees/<root_task_id> workspace")

    current = _head(workspace, timeout)
    if not current:
        raise RuntimeError("cannot resolve current HEAD")
    current_branch = _current_branch(workspace, timeout)
    if current_branch != branch:
        raise RuntimeError(f"current branch mismatch: expected {branch}, got {current_branch}")

    gate = checkpoint.get("completion_gate") or {}
    if gate.get("documentation_synchronized") is True and gate.get("operational_state_updated") is True:
        docs_head = str(checkpoint.get("docs_head") or "").strip()
        if docs_head and current == docs_head:
            return {
                "idempotent": True,
                "phase_id": phase_id,
                "code_head": code_head,
                "docs_head": docs_head,
                "graphify_ran": False,
                "changed_paths": [],
            }
        raise RuntimeError("checkpoint claims docs synchronized but current HEAD does not match docs_head")

    if current != code_head:
        raise RuntimeError(f"current HEAD must equal checkpoint integration/code head before sync: {current} != {code_head}")
    clean, detail = _git_clean(workspace, timeout)
    if not clean:
        raise RuntimeError(f"integration worktree must be clean before operational sync: {detail[:1200]}")

    resolved_base = _resolve_commit(workspace, phase_base, timeout)
    resolved_code = _resolve_commit(workspace, code_head, timeout)
    if not resolved_base or not resolved_code:
        raise RuntimeError("phase_base_commit or code_head is not resolvable")
    ancestor = _git(workspace, ["merge-base", "--is-ancestor", resolved_base, resolved_code], timeout)
    if ancestor.returncode != 0:
        raise RuntimeError("phase_base_commit is not an ancestor of code_head")

    summary_facts = [_safe_one_line(x) for x in (args.get("summary_facts") or [])]
    risks = [_safe_one_line(x) for x in (args.get("risks") or [])]
    validation_evidence = list(args.get("validation_evidence") or [])
    architecture_decisions = list(args.get("architecture_decisions") or [])
    if not summary_facts:
        raise RuntimeError("summary_facts cannot be empty")
    if not validation_evidence:
        raise RuntimeError("validation_evidence cannot be empty")
    bad_status = [x for x in validation_evidence if "PASS" not in str(x.get("status") or "").upper()]
    if bad_status:
        raise RuntimeError("all validation_evidence entries must be PASS-like for final operational sync")

    docs = cfg["docs"]
    evidence_rel = f"{str(docs['evidence_dir']).rstrip('/')}/{_slug(phase_id)}-gate-evidence.md"
    evidence_path = workspace / evidence_rel
    required_existing = [docs["estado_atual"], docs["indice"], docs["manifest_md"], docs["implementation_manifest"]]
    for rel in required_existing:
        if not (workspace / rel).is_file():
            raise RuntimeError(f"required canonical document missing: {rel}")

    evidence = _render_evidence(
        phase_id=phase_id,
        root_task_id=root_task_id,
        gate_task_id=gate_task_id,
        branch=branch,
        phase_base=resolved_base,
        code_head=resolved_code,
        architecture_revision=architecture_revision,
        summary_facts=summary_facts,
        validation_evidence=validation_evidence,
        architecture_decisions=architecture_decisions,
        risks=risks,
    )
    _write_text(evidence_path, evidence)

    estado_path = workspace / docs["estado_atual"]
    estado_text = estado_path.read_text(encoding="utf-8")
    estado_block = _render_estado_block(
        phase_id, root_task_id, gate_task_id, branch, resolved_code, architecture_revision, evidence_rel, summary_facts, risks
    )
    _write_text(estado_path, _managed_block(estado_text, "OPERATIONAL_SYNC_LATEST", estado_block))

    indice_path = workspace / docs["indice"]
    indice_text = indice_path.read_text(encoding="utf-8")
    _write_text(indice_path, _managed_block(indice_text, "OPERATIONAL_SYNC_LATEST", _render_index_block(phase_id, evidence_rel)))

    manifest_md_path = workspace / docs["manifest_md"]
    manifest_md_text = manifest_md_path.read_text(encoding="utf-8")
    _write_text(
        manifest_md_path,
        _managed_block(
            manifest_md_text,
            "OPERATIONAL_SYNC_LATEST",
            _render_manifest_md_block(phase_id, evidence_rel, root_task_id, gate_task_id, resolved_code, architecture_revision),
        ),
    )

    impl_path = workspace / docs["implementation_manifest"]
    impl_text = impl_path.read_text(encoding="utf-8")
    latest_summary = (
        f"{phase_id} (root {root_task_id}; code_head {resolved_code[:12]}; "
        f"architecture_revision {(architecture_revision or 'null')[:12]}; gate {gate_task_id}; "
        f"evidence {Path(evidence_rel).name}; push_performed false)"
    )
    impl_text = _update_unique_yaml_scalar(impl_text, "latest_verified_recorte", latest_summary)
    manifest_data = {
        "version": 1,
        "generator": f"{NAME} {VERSION}",
        "phase_id": phase_id,
        "root_task_id": root_task_id,
        "gate_task_id": gate_task_id,
        "integration_target_branch": branch,
        "phase_base_commit": resolved_base,
        "code_head": resolved_code,
        "architecture_revision": architecture_revision,
        "evidence_artifact": evidence_rel,
        "summary_facts": summary_facts,
        "risks": risks,
        "validation": validation_evidence,
        "push_performed": False,
    }
    impl_text = _replace_manifest_managed_block(impl_text, manifest_data)
    _write_text(impl_path, impl_text)

    delta_paths = _graphify_delta_paths(workspace, resolved_base, resolved_code, timeout)
    graphify_mode = str(args.get("graphify_mode") or "auto").strip().lower()
    graphify_needed = graphify_mode == "force" or _graphify_required(delta_paths)
    graphify_ran = False
    graphify_result: Optional[dict[str, Any]] = None
    if graphify_needed:
        if not bool(cfg.get("graphify_enabled", True)):
            raise RuntimeError("Graphify is required by delta but operational_sync.graphify_enabled is false")
        if graphify_runner is None:
            if not shutil.which("graphify"):
                raise RuntimeError("Graphify is required by delta but executable 'graphify' is unavailable")
            runner = lambda ws, to: _run(["graphify", "update", "."], cwd=ws, timeout=to)
        else:
            runner = graphify_runner
        gp = runner(workspace, graphify_timeout)
        graphify_result = {
            "returncode": gp.returncode,
            "stdout": (gp.stdout or "")[-3000:],
            "stderr": (gp.stderr or "")[-3000:],
        }
        if gp.returncode != 0:
            raise RuntimeError(f"graphify update failed rc={gp.returncode}: {(gp.stderr or gp.stdout)[-1200:]}")
        graphify_ran = True

    changed = _changed_paths(workspace, timeout)
    if not changed:
        raise RuntimeError("operational sync produced no changes; refusing empty docs commit")
    unexpected = [p for p in changed if not _allowed_generated_path(p, evidence_rel, cfg)]
    if unexpected:
        raise RuntimeError(f"operational sync generated out-of-scope paths; preserving for inspection: {unexpected}")

    diff_check = _git(workspace, ["diff", "--check"], timeout)
    if diff_check.returncode != 0:
        raise RuntimeError(f"git diff --check failed: {(diff_check.stdout or diff_check.stderr)[-1500:]}")

    docs_head: Optional[str] = None
    if commit:
        add_paths = [
            evidence_rel,
            docs["estado_atual"],
            docs["indice"],
            docs["manifest_md"],
            docs["implementation_manifest"],
        ]
        if any(p.startswith("graphify-out/") for p in changed):
            add_paths.append("graphify-out")
        add = _git(workspace, ["add", "--", *add_paths], timeout)
        if add.returncode != 0:
            raise RuntimeError(f"git add failed: {(add.stderr or add.stdout)[-1200:]}")
        staged = _git(workspace, ["diff", "--cached", "--name-only"], timeout)
        staged_paths = [x.strip() for x in staged.stdout.splitlines() if x.strip()]
        unexpected_staged = [p for p in staged_paths if not _allowed_generated_path(p, evidence_rel, cfg)]
        if staged.returncode != 0 or unexpected_staged:
            raise RuntimeError(f"staged scope validation failed: {unexpected_staged}")
        commit_msg = f"docs(operational): sync {phase_id} after consolidated gate"
        cp = _git(workspace, ["commit", "-m", commit_msg], timeout)
        if cp.returncode != 0:
            raise RuntimeError(f"git commit failed: {(cp.stderr or cp.stdout)[-1500:]}")
        docs_head = _head(workspace, timeout)
        clean_after, detail_after = _git_clean(workspace, timeout)
        if not clean_after:
            raise RuntimeError(f"worktree not clean after operational sync commit: {detail_after[:1200]}")

    return {
        "idempotent": False,
        "phase_id": phase_id,
        "branch": branch,
        "phase_base_commit": resolved_base,
        "code_head": resolved_code,
        "docs_head": docs_head,
        "architecture_revision": architecture_revision,
        "evidence_artifact": evidence_rel,
        "graphify_required": graphify_needed,
        "graphify_ran": graphify_ran,
        "graphify_result": graphify_result,
        "delta_paths": delta_paths,
        "changed_paths": changed,
    }


def _checkpoint_after_sync(cp: dict[str, Any], result: dict[str, Any], gate_task_id: str) -> dict[str, Any]:
    gate = dict(cp.get("completion_gate") or {})
    gate["documentation_synchronized"] = True
    gate["operational_state_updated"] = True
    gate["branch_clean"] = True
    gate["push_performed"] = False
    integrated = list(cp.get("integrated_cards") or [])
    if gate_task_id not in {str(x).split()[0] for x in integrated}:
        integrated.append(gate_task_id)
    return {
        "phase_id": cp.get("phase_id"),
        "status": cp.get("status"),
        "integration_target_branch": _normalize_ref(cp.get("integration_target_branch")),
        "phase_base_commit": result.get("phase_base_commit"),
        "architecture_revision": result.get("architecture_revision"),
        "integration_head": result.get("docs_head"),
        "code_head": result.get("code_head"),
        "docs_head": result.get("docs_head"),
        "completion_gate": gate,
        "operational_checkpoint": {
            "version": 2,
            "integration_head": result.get("docs_head"),
            "code_head": result.get("code_head"),
            "docs_head": result.get("docs_head"),
            "architecture_revision": result.get("architecture_revision"),
            "integrated_cards": integrated,
            "pending_cards": [],
            "next_expected_handoffs": [],
            "documentation": {
                "synchronized": True,
                "evidence_artifact": result.get("evidence_artifact"),
                "generated_from_code_head": result.get("code_head"),
            },
            "graphify": {
                "required": bool(result.get("graphify_required")),
                "synchronized": True,
                "generated_from_code_head": result.get("code_head"),
            },
        },
        "push_performed": False,
    }


def _finalize_handler(args: dict[str, Any]) -> str:
    root_id = str(os.environ.get("HERMES_KANBAN_TASK") or "").strip()
    workspace_raw = str(os.environ.get("HERMES_KANBAN_WORKSPACE") or "").strip()
    board = str(os.environ.get("HERMES_KANBAN_BOARD") or "").strip() or None
    gate_task_id = str(args.get("gate_task_id") or "").strip()
    if _profile() not in ALLOWED_PROFILES:
        return _err("operational_sync_finalize is orchestrator-only")
    if not root_id.startswith("t_") or not gate_task_id.startswith("t_"):
        return _err("invalid root/gate task id")
    if not workspace_raw:
        return _err("HERMES_KANBAN_WORKSPACE is required")

    cfg = _cfg()
    workspace = Path(workspace_raw).resolve(strict=False)
    workspace_root = Path(str(cfg.get("workspace_root", "/workspace"))).resolve(strict=False)
    try:
        workspace.relative_to(workspace_root)
    except ValueError:
        return _err("workspace outside configured workspace_root", workspace=str(workspace))
    if not workspace.is_dir():
        return _err("current Kanban workspace does not exist", workspace=str(workspace))

    event_base = {"event": "finalize", "root_task_id": root_id, "gate_task_id": gate_task_id, "workspace": str(workspace)}
    try:
        from hermes_cli import kanban_db as kb
        from hermes_cli import kanban_db_connect as kbc

        with kbc.connect_closing(board=board) as conn:
            root = kb.get_task(conn, root_id)
            gate_task = kb.get_task(conn, gate_task_id)
            if not root or not gate_task:
                return _err("root or gate task not found", root_task_id=root_id, gate_task_id=gate_task_id)
            if str(getattr(root, "assignee", "") or "") != "implementation-orchestrator":
                return _err("current root is not assigned to implementation-orchestrator")
            if str(getattr(gate_task, "status", "") or "") != "done":
                return _err("gate_task_id must be done before operational sync", gate_status=getattr(gate_task, "status", None))

            cp, comment_id, _ = _latest_checkpoint(conn, root_id)
            if cp is None:
                return _err("no parseable canonical operational checkpoint found on root")
            errors = _validate_checkpoint(cp, gate_task_id)
            if errors:
                _audit({**event_base, "result": "refused", "errors": errors, "checkpoint_comment_id": comment_id})
                return _err("checkpoint is not ready for deterministic operational sync", errors=errors, checkpoint_comment_id=comment_id)

            result = _synchronize_workspace(
                workspace=workspace,
                root_task_id=root_id,
                gate_task_id=gate_task_id,
                checkpoint=cp,
                args=args,
                cfg=cfg,
                commit=True,
            )
            if result.get("idempotent"):
                _audit({**event_base, "result": "idempotent", **result})
                return _ok(completion_ready=True, checkpoint_comment_id=comment_id, **result)

            post = _checkpoint_after_sync(cp, result, gate_task_id)
            comment = "OPERATIONAL_CHECKPOINT_CANONICAL v2 — operational-sync post-sync\n\n" + yaml.safe_dump(
                post,
                allow_unicode=True,
                sort_keys=False,
                default_flow_style=False,
            ).rstrip()
            kb.add_comment(conn, root_id, comment, author="operational-sync")
            _audit({**event_base, "result": "pass", "checkpoint_comment_id": comment_id, **result})
            return _ok(
                completion_ready=True,
                checkpoint_version=2,
                checkpoint_comment_published=True,
                push_performed=False,
                **result,
            )
    except subprocess.TimeoutExpired as exc:
        _audit({**event_base, "result": "timeout", "error": str(exc)[:1500]})
        return _err("operational sync command timed out", detail=str(exc)[:1500], human_required=True)
    except Exception as exc:
        _audit({**event_base, "result": "error", "error": f"{type(exc).__name__}: {exc}"[:2000]})
        return _err(f"{type(exc).__name__}: {exc}"[:2000], human_required=True)


def register(ctx: Any) -> None:
    ctx.register_tool(
        name="operational_sync_finalize",
        toolset=TOOLSET,
        schema=FINALIZE_SCHEMA,
        handler=_finalize_handler,
        check_fn=_available,
        emoji="🧾",
    )
