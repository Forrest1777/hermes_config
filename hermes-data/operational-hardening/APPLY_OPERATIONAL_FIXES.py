#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

MARKER = "HERMES_OPERATIONAL_HARDENING_2026_09_03"


class PatchError(RuntimeError):
    pass


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except Exception as exc:
        raise PatchError(f"Cannot read {path}: {exc}") from exc


def write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def require(path: Path) -> Path:
    if not path.is_file():
        raise PatchError(f"Required file not found: {path}")
    return path


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        if new in text:
            return text
        raise PatchError(f"Anchor not found for {label}")
    if text.count(old) != 1:
        raise PatchError(f"Expected one anchor for {label}, found {text.count(old)}")
    return text.replace(old, new, 1)


def regex_replace_once(text: str, pattern: str, replacement: str, label: str) -> str:
    rx = re.compile(pattern, re.S)
    matches = list(rx.finditer(text))
    if len(matches) != 1:
        raise PatchError(f"Expected one regex match for {label}, found {len(matches)}")
    return rx.sub(lambda _: replacement, text, count=1)


def patch_architect_soul(text: str) -> str:
    if f"### Protocolo de espera e retomada da aprovação ({MARKER})" in text:
        return text

    approval_anchor = (
        "A aprovação autoriza apenas as escolhas e arquivos fechados do bundle, "
        "atualização direta de contratos/fluxos/exemplos afetados, design log, "
        "validação e commit. O usuário não fornece branch, base, paths, IDs, "
        "mensagem de commit ou comandos operacionais.\n"
    )
    approval_insert = approval_anchor + f"""

### Protocolo de espera e retomada da aprovação ({MARKER})

Antes de pedir aprovação, o `recommended_bundle` deve estar operacionalmente completo: `bundle_id`, decisões, `authorized_architecture_files`, `protected_path_authorizations`, `allowed_paths`, `source_card_ids`, `affected_card_ids` e `integration_target_branch` devem conter valores concretos suficientes para a consolidação posterior. Não deixe para o orquestrador materializar campos que o próprio architect já determinou.

Ao emitir `AWAITING_USER_DECISION`:
1. publique no próprio card um comentário durável com o `recommended_bundle` completo e seu `bundle_id`;
2. use `kanban_block(kind=needs_input)` no próprio card com razão `AWAITING_ARCHITECTURE_APPROVAL bundle=<bundle_id>`;
3. encerre a run; não mantenha polling, `sleep` ou sessão viva aguardando o usuário.

A única intervenção humana normal desse gate é: registrar a aprovação/decisão no próprio card e colocar o mesmo card em `ready`. Na run seguinte, releia o próprio card/comentários, associe a aprovação explícita ao bundle pendente e continue. Uma aprovação inequívoca já persistida nunca pode gerar nova pergunta, novo `needs_input`, `ORCHESTRATOR_ACTION_REQUIRED` ou exigência de `ORCHESTRATOR_MATERIALIZATION`.
"""
    text = replace_once(text, approval_anchor, approval_insert, "architect approval protocol")

    new_gate2 = """## 8. Gate 2 — consolidação após aprovação

Após uma run ser retomada depois do gate humano:
1. releia o próprio card e seus comentários apenas o suficiente para localizar o `recommended_bundle` pendente e a aprovação explícita;
2. associe a aprovação ao `bundle_id` correto;
3. não repita perguntas ou decisões já resolvidas;
4. derive diretamente do bundle aprovado `authorized_architecture_files`, `protected_path_authorizations`, `allowed_paths`, branch, source/affected cards e demais metadados determinísticos;
5. use somente o escopo autorizado pelo bundle;
6. releia apenas as fontes afetadas necessárias à consolidação; não refaça discovery já concluído por ritual;
7. atualize documentos e referências necessárias;
8. preserve decisões não afetadas e pendências ainda abertas;
9. registre decisão e aprovação no design log;
10. valide escopo/coerência;
11. crie commit atômico;
12. retorne `READY_FOR_INTEGRATION`.

`ORCHESTRATOR_MATERIALIZATION` não faz parte do fluxo normal e `ORCHESTRATOR_ACTION_REQUIRED` não deve ser emitido para campos que o architect já determinou no bundle. Se, apesar do gate 1, um campo operacional realmente não puder ser derivado do bundle aprovado, trate isso como defeito operacional do próprio pacote: preserve a aprovação, reporte `BLOCKED_OPERATIONAL` ao orquestrador com o campo exato ausente e não peça ao usuário para decidir novamente.

A aprovação humana é o único gate manual normal. Depois de `ready`, o fluxo deve seguir até `READY_FOR_INTEGRATION` sem nova intervenção humana, salvo novo problema material diferente do que já foi aprovado.
"""
    text = regex_replace_once(
        text,
        r"## 8\. Gate 2 — consolidação\n.*?(?=\n## 9\. Design log)",
        new_gate2,
        "architect gate 2",
    )
    return text


def patch_orchestrator_soul(text: str) -> str:
    if f"### Disciplina de contexto e budget ({MARKER})" in text:
        return text

    fast_resume_anchor = (
        "5. se divergir, abandone fast resume e faça bootstrap completo.\n\n"
        "Não reconstrua estado pelo histórico de conversa."
    )
    fast_resume_new = (
        "5. se divergir, abandone fast resume e faça bootstrap completo.\n\n"
        f"### Disciplina de contexto e budget ({MARKER})\n"
        "- prefira referências duráveis (`card_id`, commit, path, operation_id, test id) a colar logs/código extensos em handoffs;\n"
        "- depois que uma fonte foi localizada e validada, releia somente o delta ou o trecho necessário;\n"
        "- não repita discovery, `git status`, Graphify, documentação ou testes apenas para reconstruir contexto já persistido;\n"
        "- se uma saída de tool for grande, retenha resumo + identificadores e consulte detalhes sob demanda.\n\n"
        "Não reconstrua estado pelo histórico de conversa."
    )
    text = replace_once(text, fast_resume_anchor, fast_resume_new, "orchestrator context budget")

    admin_new = """### Controle administrativo estreito de cards da própria fase

A promoção normal continua dependency-driven. O fluxo de aprovação do `implementation-architect` NÃO usa `ORCHESTRATOR_MATERIALIZATION` e NÃO exige `orchestration_unblock_card` como etapa normal.

Quando um architect chegar a `AWAITING_USER_DECISION`:
1. preserve o card architect como pré-requisito do root/fluxo afetado;
2. atualize `operational_checkpoint.next_expected_handoffs`;
3. bloqueie o root por dependência e encerre a run;
4. não faça polling e não tente materializar os campos do bundle;
5. o humano registra a aprovação e coloca o próprio card architect em `ready`;
6. o dispatcher retoma o architect; ele deve consolidar sozinho até `READY_FOR_INTEGRATION`;
7. o root volta somente pelo mecanismo normal de dependências/handoff.

`orchestration_unblock_card` fica reservado a recovery operacional excepcional de card relacionado, nunca como parte obrigatória do gate de arquitetura. Não use CLI Kanban/SQL como workaround.

"""
    text = regex_replace_once(
        text,
        r"### Controle administrativo estreito de cards da própria fase\n.*?(?=## 6\. Gate de executabilidade)",
        admin_new,
        "orchestrator admin control",
    )

    route_b_old_pattern = (
        r"Preserve cards/worktrees, repasse checkpoint/fontes/arquivos, aguarde "
        r"`CONTRACT_CORRECTION`, `AWAITING_USER_DECISION`, `ORCHESTRATOR_ACTION_REQUIRED` ou `READY_FOR_INTEGRATION`\. "
        r"Usuário participa apenas se houver escolha material\.\n\n"
        r"Se o architect retornar `ORCHESTRATOR_ACTION_REQUIRED` após uma aprovação já registrada:.*?"
        r"(?=Depois do `READY_FOR_INTEGRATION` arquitetural:)"
    )
    route_b_new = """Preserve cards/worktrees, repasse checkpoint/fontes/arquivos e aguarde `CONTRACT_CORRECTION`, `AWAITING_USER_DECISION` ou `READY_FOR_INTEGRATION`. Usuário participa apenas se houver escolha material.

Se o architect retornar `AWAITING_USER_DECISION`, confirme que o bundle está persistido, mantenha o vínculo de dependência, bloqueie o root por dependência e encerre a run. Não materialize paths/IDs em nome do architect e não faça polling. Depois da aprovação humana e do `ready` do próprio architect, o fluxo normal do dispatcher deve levá-lo diretamente à consolidação.

"""
    text = regex_replace_once(text, route_b_old_pattern, route_b_new, "orchestrator architect route")

    validation_new = """## 10. Validação consolidada

Política de testes:
1. durante microajustes/iteração de implementação, workers executam somente testes impactados/focados;
2. no fechamento de um card ou subsistema, execute a suíte do subsistema apenas quando necessária para validar o contrato afetado;
3. o full gate consolidado é obrigatório no fechamento final da integração/fase;
4. depois de um full gate verde, não repita a suíte completa sem mudança posterior que possa invalidá-la;
5. se o full gate falhar, corrija usando testes focados e repita somente validações invalidadas durante o diagnóstico; execute novamente o full gate uma vez no novo estado integrado final;
6. LSP/runtime somente quando aplicáveis;
7. falha => card corretivo delimitado.

O objetivo é preservar a barreira global final sem pagar o custo da suíte completa após cada microajuste.

O orquestrador não assume lifecycle GWRM do gate.
"""
    text = regex_replace_once(
        text,
        r"## 10\. Validação consolidada\n.*?O orquestrador não assume lifecycle GWRM do gate\.\n",
        validation_new,
        "orchestrator test policy",
    )

    return text


def patch_worker_soul(text: str) -> str:
    if MARKER in text and "GWRM GUI policy" in text:
        return text

    old_dirty = (
        "8. confirme que a worktree está limpa no início; sujeira preexistente não atribuível à execução atual "
        "é `BLOCKED_OPERATIONAL` e deve ser preservada, sem `reset/clean`;"
    )
    new_dirty = (
        "8. worktree suja no início é `BLOCKED_OPERATIONAL` por padrão. A única exceção é quando "
        "`worktree_guardian_verify` retornar explicitamente `authorized_retry_checkpoint=true` com "
        "`resume_epoch` válido para esta run; nesse caso preserve exatamente o WIP existente e trate-o como "
        "checkpoint autorizado, sem `reset/clean` e sem exigir worktree inicialmente limpa;"
    )
    text = replace_once(text, old_dirty, new_dirty, "worker authorized retry checkpoint")

    gui_anchor = "Falha de activate => consulte status uma vez. Não reative se `starting/ready`; nova tentativa só após estado terminal e evidência de falha transitória. Nunca use operação Godot sem `ready`.\n"
    gui_insert = gui_anchor + f"""

### GWRM GUI policy ({MARKER})

Validação gráfica de Godot/Windows usa exclusivamente as tools MCP `mcp__gwrm__gui_*`. Nunca use o toolset nativo `computer_*`, X11, `DISPLAY`, AT-SPI ou desktop Linux como fallback.

Fluxo gráfico: `gui_wait_for_window` → `gui_inspect_window` (semantic-first) → `gui_wait_for_element`/ação semântica quando disponível → `gui_capture_window` apenas como fallback visual → ação window-local (`gui_click`, `gui_type_text`, `gui_press_key`, `gui_hotkey`, `gui_scroll`) → verificação de estado → `stop_project`/cleanup.

Se a inspeção semântica não expuser controles internos do Godot, isso não é falha: use captura visual e coordenadas relativas à janela autorizada pelo GWRM. Não tente mudar de backend de Computer Use.
"""
    text = replace_once(text, gui_anchor, gui_insert, "worker GUI policy")

    gut_anchor = "Do not rerun the same failing GUT without new evidence, implementation progress, or an explicit justified `force_rerun=true`.\n\nDirect GWRM tools remain diagnostic/recovery surfaces only."
    gut_new = f"""Do not rerun the same failing GUT without new evidence, implementation progress, or an explicit justified `force_rerun=true`.

### Test impact policy ({MARKER})

- microajuste/iteração: execute somente o menor conjunto de testes impactados pelo arquivo/contrato alterado;
- fechamento do card/subsistema: amplie para a suíte do subsistema quando isso agregar cobertura material;
- full suite: não é ritual do worker após cada mudança; é obrigatória no gate consolidado final coordenado pelo orchestrator ou quando o card explicitamente for o card de full gate;
- após um full gate verde, não o repita no mesmo Git state;
- após falha de full gate, use testes focados durante a correção e só volte ao full gate quando houver novo estado integrado que precise ser certificado.

Direct GWRM tools remain diagnostic/recovery surfaces only."""
    text = replace_once(text, gut_anchor, gut_new, "worker test impact policy")
    return text


def execution_governor_soul() -> str:
    return f"""# Execution Governor

For a dispatcher-owned board-level Governance run (the same persistent card may run many times):
1. Call `governance_context` exactly once.
2. Using only its returned facts, reply exactly:
`DECISION=RETRY_AUTHORIZED|HUMAN_REQUIRED; REASON=<short reason>`

RETRY_AUTHORIZED only when:
- attempts remain;
- provider circuit is CLOSED;
- progress is not explicitly false; and
- the failure is transient, such as `timed_out`, timeout, temporary network failure, or HTTP 5xx.

Provider budget/quota exhaustion is handled deterministically by the provider-wait scheduler in `governance-guard` ({MARKER}) and should normally never be sent to this LLM decision loop. If such a case does appear, use HUMAN_REQUIRED only when automatic provider recovery is disabled/exhausted or state is unsafe; never bypass an OPEN/PROBING provider circuit manually.

HUMAN_REQUIRED for configuration/model/payload errors, exhausted ordinary attempt budget, provider recovery exhausted/disabled, unsafe/corrupt state, or `progress=false`.

Do not call Kanban tools. Do not implement work. The plugin validates the line and performs lifecycle/state mutations.

The Governance card is persistent per board. Process only the single case returned by `governance_context` for the current run; never reason about or batch other queued cases.
"""


def policy_yaml() -> str:
    return """schema_version: 1
timezone: America/Sao_Paulo
enabled: true
governed_profiles:
- implementation-orchestrator
- implementation-worker
- implementation-architect
- execution-governor
task_budget:
  default_max_total_attempts: 2
  hold_on_first_unexpected_failure: true
progress:
  compare_git_head: true
  compare_git_status: true
  human_required_when_no_material_progress: true
provider_circuit:
  terminal_error_types:
  - usage_limit_reached
  - insufficient_quota
  - billing_hard_limit_reached
  - usage_quota_exceeded
  - quota_exceeded
  require_human_reset: false
  retry_terminal_errors: true
  fallback_terminal_errors: false
provider_recovery:
  enabled: true
  retry_interval_minutes: 20
  max_automatic_retries: 12
  prefer_provider_reset_time: false
  authorization_ttl_minutes: 60
  one_probe_per_provider_per_dispatch_tick: true
kanban_actions:
  enforce_boards:
  - default
  mode: enforce
  hold_block_kind: needs_input
  governance_cards:
    auto_create: true
    auto_create_boards:
    - default
    assignee: execution-governor
    idempotency_prefix: execution-governance
    max_runtime_seconds: 300
    initial_status: ready
    auto_create_tenants: []
    max_retries: 1
  enforce_tenants: []
"""


def godot_skill() -> str:
    return f"""---
name: godot-development
description: Desenvolvimento e verificacao de worktrees Godot/GDScript usando GWRM, LSP, GUT e GUI Windows supervisionada.
---

# Godot/GDScript no Hermes com GWRM

## Inicio e encerramento

1. Valide a worktree já provisionada pelo dispatcher com a skill `worktree-preflight`.
2. Resolva `worktree_name` a partir do basename de `HERMES_KANBAN_WORKSPACE`.
3. Chame `activate_worktree` e aguarde `status: ready` antes de trabalhar com runtime Godot.
4. Use sempre a mesma `worktree_name` nas tools Godot, GUT e GUI.
5. Chame `stop_project` se esta run iniciou o projeto.
6. Chame `deactivate_worktree` antes de concluir ou devolver o card e confirme `stopped`, sem PIDs residuais.

O worker nao inicia Godot fora do GWRM, nao escolhe portas e nao converte caminhos manualmente.

## Caminhos

- Use `res://` para recursos do projeto.
- Use `user://` para dados gravaveis.
- Nao grave caminhos absolutos de Windows ou Linux no codigo, cenas ou recursos.
- Caminhos absolutos pertencem apenas a configuracao externa do GWRM.

## Validacao

1. Edite os arquivos da worktree associada ao card.
2. Leia e corrija diagnosticos LSP relevantes.
3. Use `godot_lsp_status` ou `get_worktree_status` quando houver falha de conexao.
4. Execute GUT no mesmo `worktree_name`.
5. Microajustes usam testes impactados/focados; a suíte completa fica reservada ao gate consolidado final ou a um card explicitamente criado para esse gate.
6. Nao trate ausencia de diagnosticos como prova de correcao de runtime.
7. Nao considere GUT aprovado sem scripts, testes e assertagens maiores que zero e sem falhas/erros.
8. Nao repita full suite no mesmo Git state depois de um PASS.

## GUI Windows / Computer Use ({MARKER})

Para uma janela Godot gráfica no Windows, use somente `mcp__gwrm__gui_*`:
- `gui_status`
- `gui_list_windows`
- `gui_wait_for_window`
- `gui_inspect_window`
- `gui_capture_window`
- `gui_wait_for_element`
- `gui_click`
- `gui_type_text`
- `gui_press_key`
- `gui_hotkey`
- `gui_scroll`

Nunca use Hermes native `computer_*`, X11, `DISPLAY`, AT-SPI ou Linux desktop CUA para esse ambiente.

Política: semantic-first. Se a árvore semântica não expuser controles internos do Godot, use `gui_capture_window` como fallback visual e interaja por coordenadas relativas à janela autorizada. Capture evidência anterior/posterior quando o card exigir gate gráfico.

## Separacao

- LSP: sintaxe, tipos, simbolos, referencias e diagnosticos semanticos.
- Godot MCP dedicado/GWRM: cenas, nos, recursos, execucao e logs da worktree.
- GUT: comportamento e integracao no projeto da worktree.
- GWRM: lifecycle, importacao, portas, processos, mapeamento, GUI Windows e cleanup.
"""


def patch_config_remove_native_computer(text: str) -> str:
    marker = "    - computer_use\n"
    if marker in text:
        text = text.replace(marker, "", 1)
    return text


def patch_worker_config(text: str) -> str:
    text = patch_config_remove_native_computer(text)
    if "        - gui_status\n" not in text:
        anchor = "        - update_project_uids\n"
        gui = """        - update_project_uids
        - gui_status
        - gui_list_windows
        - gui_wait_for_window
        - gui_inspect_window
        - gui_capture_window
        - gui_wait_for_element
        - gui_click
        - gui_type_text
        - gui_press_key
        - gui_hotkey
        - gui_scroll
"""
        if anchor not in text:
            raise PatchError("Worker config: update_project_uids anchor not found")
        text = text.replace(anchor, gui, 1)
    return text


def patch_execution_governance(text: str) -> str:
    if "retry_checkpoint_authorized" in text and MARKER in text:
        return text

    if "import time\n" not in text:
        text = replace_once(text, "import tempfile\n", "import tempfile\nimport time\n", "execution-governance import time")

    helper = f'''\n\n# {MARKER}: durable authorization for a preserved dirty checkpoint.\ndef _retry_authorization_ttl_seconds(policy: dict[str, Any]) -> int:\n    cfg = policy.get("provider_recovery") or {{}}\n    try:\n        minutes = max(5, int(cfg.get("authorization_ttl_minutes") or 60))\n    except Exception:\n        minutes = 60\n    return minutes * 60\n\n\ndef _authorize_retry_checkpoint(case: dict[str, Any], rationale: str) -> dict[str, Any] | None:\n    attempt = case.get("attempt") or {{}}\n    after = attempt.get("after") or {{}}\n    if not isinstance(after, dict) or not after.get("available"):\n        return None\n    try:\n        changed = int(after.get("changed_entries") or 0)\n    except Exception:\n        changed = 0\n    if changed <= 0:\n        return None\n    workspace = _norm(after.get("path"))\n    head = _norm(after.get("head"))\n    status_sha256 = _norm(after.get("status_sha256"))\n    if not workspace or not head or not status_sha256:\n        return None\n\n    policy = _policy()\n    now_epoch = int(time.time())\n    path = _root() / "state.json"\n    with _lock(path):\n        state = _read_state_path(path)\n        task = state.setdefault("tasks", {{}}).setdefault(\n            _norm(case.get("task_id")), {{"total_attempts": 0, "runs": {{}}}}\n        )\n        resume_epoch = int(task.get("resume_epoch") or 0) + 1\n        authorization = {{\n            "state": "AUTHORIZED",\n            "reason": "governance_retry",\n            "case_id": _norm(case.get("case_id")),\n            "resume_epoch": resume_epoch,\n            "workspace": workspace,\n            "head": head,\n            "status_sha256": status_sha256,\n            "authorized_at": now_epoch,\n            "expires_at": now_epoch + _retry_authorization_ttl_seconds(policy),\n            "rationale": rationale[:500],\n        }}\n        task["resume_epoch"] = resume_epoch\n        task["authorized_recovery_checkpoint"] = authorization\n        state["last_updated_at"] = _now()\n        _atomic_json(path, state)\n    _append_event(\n        "retry_checkpoint_authorized",\n        task_id=case.get("task_id"),\n        case_id=case.get("case_id"),\n        resume_epoch=resume_epoch,\n        workspace=workspace,\n    )\n    return authorization\n\n\ndef _revoke_retry_checkpoint(task_id: str, case_id: str) -> None:\n    path = _root() / "state.json"\n    with _lock(path):\n        state = _read_state_path(path)\n        task = (state.get("tasks") or {{}}).get(task_id)\n        if isinstance(task, dict):\n            auth = task.get("authorized_recovery_checkpoint")\n            if isinstance(auth, dict) and _norm(auth.get("case_id")) == case_id:\n                task.pop("authorized_recovery_checkpoint", None)\n                state["last_updated_at"] = _now()\n                _atomic_json(path, state)\n'''
    text = replace_once(text, "\n\ndef governance_decide(args: dict, **kwargs) -> str:\n", helper + "\n\ndef governance_decide(args: dict, **kwargs) -> str:\n", "execution-governance retry helper")

    validation_anchor = '''        if _norm(last_error.get("reason")) in {"model_not_found", "invalid_request", "payload_too_large"}:\n            return _err("retry denied: non-retryable/configuration error", case_id=case_id)\n\n    try:\n'''
    validation_new = '''        if _norm(last_error.get("reason")) in {"model_not_found", "invalid_request", "payload_too_large"}:\n            return _err("retry denied: non-retryable/configuration error", case_id=case_id)\n\n    retry_authorization = None\n    if decision == "RETRY_AUTHORIZED":\n        try:\n            retry_authorization = _authorize_retry_checkpoint(case, rationale)\n        except Exception as exc:\n            return _err(\n                "retry checkpoint authorization failed",\n                case_id=case_id,\n                detail=f"{type(exc).__name__}: {exc}"[:1000],\n            )\n\n    try:\n'''
    text = replace_once(text, validation_anchor, validation_new, "execution-governance authorize before unblock")

    old_retry_branch = '''            if decision == "RETRY_AUTHORIZED":\n                if str(task.status) != "blocked":\n                    return _err("original task must be blocked before retry", case_id=case_id, task_status=str(task.status))\n                kb.add_comment(conn, task_id, author="execution-governor",\n                               body=f"RETRY_AUTHORIZED. Case {case_id}. {rationale}")\n                if not kb.unblock_task(conn, task_id):\n                    return _err("Hermes refused to unblock original task", case_id=case_id)\n'''
    new_retry_branch = '''            if decision == "RETRY_AUTHORIZED":\n                if str(task.status) != "blocked":\n                    if retry_authorization:\n                        _revoke_retry_checkpoint(task_id, case_id)\n                    return _err("original task must be blocked before retry", case_id=case_id, task_status=str(task.status))\n                epoch_note = (\n                    f" Resume epoch {retry_authorization.get('resume_epoch')}."\n                    if isinstance(retry_authorization, dict)\n                    else ""\n                )\n                kb.add_comment(conn, task_id, author="execution-governor",\n                               body=f"RETRY_AUTHORIZED. Case {case_id}. {rationale}{epoch_note}")\n                if not kb.unblock_task(conn, task_id):\n                    if retry_authorization:\n                        _revoke_retry_checkpoint(task_id, case_id)\n                    return _err("Hermes refused to unblock original task", case_id=case_id)\n'''
    text = replace_once(text, old_retry_branch, new_retry_branch, "execution-governance retry branch")
    return text


def patch_retry_checkpoint_guard(text: str) -> str:
    if "authorized_retry_checkpoint_allowed" in text and MARKER in text:
        return text
    if "import hashlib\n" not in text:
        text = replace_once(text, "import json\n", "import hashlib\nimport json\nimport os\n", "retry guard imports")

    old_status_return = '''    entries = [\n        line\n        for line in proc.stdout.splitlines()\n        if line.strip()\n    ]\n\n    return {\n        "ok": True,\n        "entries": entries,\n    }\n'''
    new_status_return = '''    entries = [\n        line\n        for line in proc.stdout.splitlines()\n        if line.strip()\n    ]\n    status_text = proc.stdout or ""\n    head_proc = subprocess.run(\n        ["git", "-C", str(workspace), "rev-parse", "HEAD"],\n        capture_output=True,\n        text=True,\n        encoding="utf-8",\n        errors="replace",\n        timeout=20,\n        check=False,\n    )\n    head = head_proc.stdout.strip() if head_proc.returncode == 0 else None\n\n    return {\n        "ok": True,\n        "entries": entries,\n        "head": head,\n        "status_sha256": hashlib.sha256(status_text.encode("utf-8")).hexdigest(),\n    }\n'''
    text = replace_once(text, old_status_return, new_status_return, "retry guard status fingerprint")

    helper = f'''\n\n# {MARKER}: allow exactly one governance-authorized dirty retry fingerprint.\ndef _governor_state_path() -> Path:\n    try:\n        from hermes_constants import get_hermes_home\n        home = Path(get_hermes_home()).resolve(strict=False)\n    except Exception:\n        home = Path(os.environ.get("HERMES_HOME", "/opt/data")).resolve(strict=False)\n    if home.name == "execution-governor":\n        governor = home\n    elif home.parent.name == "profiles":\n        governor = home.parent / "execution-governor"\n    else:\n        governor = home / "profiles" / "execution-governor"\n    return governor / "governance" / "state.json"\n\n\ndef _authorized_recovery(task_id: str, workspace: Path, status: dict[str, Any]) -> dict[str, Any] | None:\n    try:\n        state = json.loads(_governor_state_path().read_text(encoding="utf-8"))\n        task = (state.get("tasks") or {{}}).get(task_id) or {{}}\n        auth = task.get("authorized_recovery_checkpoint")\n        if not isinstance(auth, dict) or auth.get("state") != "AUTHORIZED":\n            return None\n        if int(auth.get("expires_at") or 0) < int(time.time()):\n            return None\n        if str(Path(str(auth.get("workspace") or "")).resolve(strict=False)) != str(workspace.resolve(strict=False)):\n            return None\n        if str(auth.get("head") or "") != str(status.get("head") or ""):\n            return None\n        if str(auth.get("status_sha256") or "") != str(status.get("status_sha256") or ""):\n            return None\n        return auth\n    except Exception:\n        return None\n'''
    text = replace_once(text, "\n\ndef _inspect_retry_checkpoint(\n", helper + "\n\ndef _inspect_retry_checkpoint(\n", "retry guard authorization helper")

    dirty_return_anchor = '''    entries = status["entries"]\n\n    if not entries:\n        return None\n\n    return {\n        "task_id": task_id,\n        "workspace": str(workspace),\n        "run_count": run_count,\n        "guard_reason":\n            "dirty_retry_checkpoint",\n        "entries": entries,\n    }\n'''
    dirty_return_new = '''    entries = status["entries"]\n\n    if not entries:\n        return None\n\n    authorization = _authorized_recovery(task_id, workspace, status)\n    if authorization is not None:\n        return {\n            "task_id": task_id,\n            "workspace": str(workspace),\n            "run_count": run_count,\n            "guard_reason": "authorized_retry_checkpoint",\n            "authorized_recovery": True,\n            "resume_epoch": authorization.get("resume_epoch"),\n            "entries": entries,\n        }\n\n    return {\n        "task_id": task_id,\n        "workspace": str(workspace),\n        "run_count": run_count,\n        "guard_reason":\n            "dirty_retry_checkpoint",\n        "entries": entries,\n    }\n'''
    text = replace_once(text, dirty_return_anchor, dirty_return_new, "retry guard inspect authorization")

    remember_anchor = '''    if state is None:\n        return None\n\n    _remember(state)\n'''
    remember_new = '''    if state is None:\n        return None\n\n    if state.get("authorized_recovery"):\n        _audit({\n            "event": "authorized_retry_checkpoint_allowed",\n            **state,\n            "entries": state.get("entries", [])[:50],\n        })\n        return None\n\n    _remember(state)\n'''
    text = replace_once(text, remember_anchor, remember_new, "retry guard allow authorized")

    flush_anchor = '''            if fresh is None:\n                handled.append(task_id)\n                continue\n\n            reason = _build_reason(\n'''
    flush_new = '''            if fresh is None:\n                handled.append(task_id)\n                continue\n\n            if fresh.get("authorized_recovery"):\n                _audit({\n                    "event": "authorized_retry_checkpoint_allowed",\n                    **fresh,\n                    "entries": fresh.get("entries", [])[:50],\n                })\n                handled.append(task_id)\n                continue\n\n            reason = _build_reason(\n'''
    text = replace_once(text, flush_anchor, flush_new, "retry guard flush authorized")
    return text


def patch_worktree_guardian(text: str) -> str:
    if "authorized_retry_checkpoint" in text and MARKER in text:
        return text
    if "import hashlib\n" not in text:
        text = replace_once(text, "import json\n", "import hashlib\nimport json\n", "worktree guardian hashlib")

    helper = f'''\n\n# {MARKER}: read-only validation of a governance-authorized dirty retry.\ndef _governor_state_path() -> Path:\n    try:\n        from hermes_constants import get_hermes_home\n        home = Path(get_hermes_home()).resolve(strict=False)\n    except Exception:\n        home = Path(os.environ.get("HERMES_HOME", "/opt/data")).resolve(strict=False)\n    if home.name == "execution-governor":\n        governor = home\n    elif home.parent.name == "profiles":\n        governor = home.parent / "execution-governor"\n    else:\n        governor = home / "profiles" / "execution-governor"\n    return governor / "governance" / "state.json"\n\n\ndef _authorized_retry_checkpoint(workspace: Path, head: str, status_sha256: str) -> dict[str, Any] | None:\n    task_id = str(os.environ.get("HERMES_KANBAN_TASK") or "")\n    if not task_id:\n        return None\n    try:\n        state = json.loads(_governor_state_path().read_text(encoding="utf-8"))\n        task = (state.get("tasks") or {{}}).get(task_id) or {{}}\n        auth = task.get("authorized_recovery_checkpoint")\n        if not isinstance(auth, dict):\n            return None\n        auth_state = str(auth.get("state") or "")\n        if auth_state not in {{"AUTHORIZED", "IN_USE"}}:\n            return None\n        if int(auth.get("expires_at") or 0) < int(time.time()):\n            return None\n        if str(Path(str(auth.get("workspace") or "")).resolve(strict=False)) != str(workspace.resolve(strict=False)):\n            return None\n        if str(auth.get("head") or "") != head:\n            return None\n        if str(auth.get("status_sha256") or "") != status_sha256:\n            return None\n        if auth_state == "IN_USE":\n            current_run = str(os.environ.get("HERMES_KANBAN_RUN_ID") or "")\n            if current_run and str(auth.get("run_id") or "") not in {{"", current_run}}:\n                return None\n        return auth\n    except Exception:\n        return None\n'''
    text = replace_once(text, "\n\ndef _validate_worktree(\n", helper + "\n\ndef _validate_worktree(\n", "worktree guardian auth helper")

    status_update_anchor = '''                "base_ref": base_ref or None,\n                "initial_git_clean": not bool(status.stdout.strip()),\n            }\n        )\n'''
    status_update_new = '''                "base_ref": base_ref or None,\n                "initial_git_clean": not bool(status.stdout.strip()),\n                "git_status_sha256": hashlib.sha256((status.stdout or "").encode("utf-8")).hexdigest(),\n                "authorized_retry_checkpoint": False,\n                "resume_epoch": None,\n            }\n        )\n'''
    text = replace_once(text, status_update_anchor, status_update_new, "worktree guardian status hash")

    dirty_anchor = '''        if status.stdout.strip():\n            result["errors"].append("worktree is not clean")\n'''
    dirty_new = '''        if status.stdout.strip():\n            authorization = _authorized_retry_checkpoint(\n                real,\n                head_sha,\n                result["git_status_sha256"],\n            )\n            if authorization is not None:\n                result["authorized_retry_checkpoint"] = True\n                result["resume_epoch"] = authorization.get("resume_epoch")\n                result["retry_checkpoint_reason"] = authorization.get("reason")\n            else:\n                result["errors"].append("worktree is not clean")\n'''
    text = replace_once(text, dirty_anchor, dirty_new, "worktree guardian dirty authorization")
    return text


def patch_governance_guard(text: str) -> str:
    if "provider_wait_resumed" in text and MARKER in text:
        return text

    provider_block_pattern = r"def _open_provider_circuit\(provider: str, model: str \| None, info: dict\[str, Any\]\) -> None:\n.*?(?=\n\ndef _classify_api_error)"
    provider_block_new = f'''# {MARKER}: provider budget is a deferred retry state, not an immediate human gate.\ndef _provider_recovery_config(policy: dict[str, Any] | None = None) -> dict[str, Any]:\n    raw = ((policy or _load_policy()).get("provider_recovery") or {{}})\n    try:\n        interval_minutes = max(1, int(raw.get("retry_interval_minutes") or 20))\n    except Exception:\n        interval_minutes = 20\n    try:\n        max_retries = max(0, int(raw.get("max_automatic_retries") or 12))\n    except Exception:\n        max_retries = 12\n    try:\n        ttl_minutes = max(5, int(raw.get("authorization_ttl_minutes") or 60))\n    except Exception:\n        ttl_minutes = 60\n    return {{\n        "enabled": bool(raw.get("enabled", True)),\n        "retry_interval_seconds": interval_minutes * 60,\n        "max_automatic_retries": max_retries,\n        "prefer_provider_reset_time": bool(raw.get("prefer_provider_reset_time", False)),\n        "authorization_ttl_seconds": ttl_minutes * 60,\n        "one_probe_per_provider_per_dispatch_tick": bool(\n            raw.get("one_probe_per_provider_per_dispatch_tick", True)\n        ),\n    }}\n\n\ndef _next_provider_retry_at(info: dict[str, Any], recovery: dict[str, Any]) -> int:\n    now_epoch = int(time.time())\n    reset = info.get("resets_at")\n    if recovery.get("prefer_provider_reset_time") and reset is not None:\n        try:\n            reset_epoch = int(reset)\n            if reset_epoch > now_epoch:\n                return reset_epoch\n        except Exception:\n            pass\n    return now_epoch + int(recovery.get("retry_interval_seconds") or 1200)\n\n\ndef _open_provider_circuit(provider: str, model: str | None, info: dict[str, Any]) -> None:\n    task_id = _task_id()\n    local_reset, tz_name = _format_reset(info.get("resets_at"))\n    recovery = _provider_recovery_config()\n    next_retry_at = _next_provider_retry_at(info, recovery) if recovery.get("enabled") else None\n\n    def mutate(state):\n        prior = (state.setdefault("providers", {{}}).get(provider) or {{}})\n        state["providers"][provider] = {{\n            "state": "OPEN",\n            "reason": info.get("error_type") or "provider_budget_exhausted",\n            "model": model,\n            "opened_at": _utc_now(),\n            "opened_count": int(prior.get("opened_count") or 0) + 1,\n            "task_id": task_id,\n            "plan_type": info.get("plan_type"),\n            "resets_at": info.get("resets_at"),\n            "resets_at_local": local_reset,\n            "timezone": tz_name,\n            "next_retry_at": next_retry_at,\n            "human_reset_required": not bool(recovery.get("enabled")),\n        }}\n\n    _update_state(mutate)\n    _append_event(\n        "provider_circuit_opened",\n        provider=provider,\n        model=model,\n        task_id=task_id,\n        reason=info.get("error_type"),\n        plan_type=info.get("plan_type"),\n        resets_at=info.get("resets_at"),\n        resets_at_local=local_reset,\n        timezone=tz_name,\n        next_retry_at=next_retry_at,\n        automatic_recovery=bool(recovery.get("enabled")),\n    )\n'''
    text = regex_replace_once(text, provider_block_pattern, provider_block_new, "governance provider circuit")

    old_message = '''    parts.extend([\n        "Automatic retries: DISABLED",\n        "Provider circuit: OPEN",\n        "Human intervention required before this provider is reused.",\n    ])\n    return {\n        "reason": "billing",\n        "retryable": False,\n        "should_compress": False,\n        "should_rotate_credential": False,\n        "should_fallback": False,\n        "message": "\\n".join(parts),\n        "error_context": {\n            "governance": "provider_budget_exhausted",\n            "provider": provider_name,\n            "model": model,\n            "plan_type": info.get("plan_type"),\n            "resets_at": info.get("resets_at"),\n            "resets_at_local": reset_text,\n            "timezone": tz_name,\n            "human_intervention_required": True,\n            "state_unavailable": bool(state_unavailable),\n        },\n    }\n'''
    new_message = '''    recovery = _provider_recovery_config()\n    next_retry_at = _next_provider_retry_at(info, recovery) if recovery.get("enabled") else None\n    if recovery.get("enabled"):\n        parts.extend([\n            "Immediate retries: DISABLED",\n            "Provider circuit: OPEN / WAITING_PROVIDER",\n            f"Automatic recovery scheduled for epoch {next_retry_at}.",\n        ])\n    else:\n        parts.extend([\n            "Automatic retries: DISABLED",\n            "Provider circuit: OPEN",\n            "Human intervention required before this provider is reused.",\n        ])\n    return {\n        "reason": "billing",\n        "retryable": False,\n        "should_compress": False,\n        "should_rotate_credential": False,\n        "should_fallback": False,\n        "message": "\\n".join(parts),\n        "error_context": {\n            "governance": "provider_budget_exhausted",\n            "provider": provider_name,\n            "model": model,\n            "plan_type": info.get("plan_type"),\n            "resets_at": info.get("resets_at"),\n            "resets_at_local": reset_text,\n            "timezone": tz_name,\n            "next_retry_at": next_retry_at,\n            "automatic_recovery_enabled": bool(recovery.get("enabled")),\n            "human_intervention_required": not bool(recovery.get("enabled")),\n            "state_unavailable": bool(state_unavailable),\n        },\n    }\n'''
    text = replace_once(text, old_message, new_message, "governance provider classification message")

    helper_anchor = "\n\ndef _on_worker_exited_factory(ctx):\n"
    helper = f'''\n\n# {MARKER}: deterministic provider-wait lifecycle.\ndef _update_case_status_file(case_id: str, status: str, **metadata: Any) -> None:\n    path = _governance_dir() / "cases" / f"{{case_id}}.json"\n    try:\n        with _file_lock(path):\n            data = json.loads(path.read_text(encoding="utf-8"))\n            if isinstance(data, dict):\n                data["status"] = status\n                if metadata:\n                    data.setdefault("provider_recovery", {{}}).update(metadata)\n                _atomic_json_write(path, data)\n    except Exception as exc:\n        _append_event("provider_case_file_update_failed", case_id=case_id, error=str(exc)[:800])\n\n    def mutate(state):\n        entry = (state.get("cases") or {{}}).get(case_id)\n        if isinstance(entry, dict):\n            entry["status"] = status\n    try:\n        _update_state(mutate)\n    except Exception:\n        pass\n\n\ndef _comment_task(board: str | None, task_id: str, body: str) -> None:\n    try:\n        from hermes_cli import kanban_db as kb\n        conn = kb.connect(board=_normalized_board(board))\n        try:\n            kb.add_comment(conn, task_id, author="execution-governance", body=body)\n        finally:\n            conn.close()\n    except Exception as exc:\n        _append_event("provider_wait_comment_failed", task_id=task_id, error=str(exc)[:800])\n\n\ndef _arm_provider_wait(ctx, case: dict[str, Any], board: str | None, policy: dict[str, Any]) -> dict[str, Any] | None:\n    recovery = _provider_recovery_config(policy)\n    circuit = case.get("provider_circuit") or {{}}\n    if not recovery.get("enabled") or circuit.get("state") != "OPEN":\n        return None\n    provider = str(circuit.get("provider") or ((case.get("last_api_error") or {{}}).get("provider")) or "").strip()\n    if not provider:\n        return None\n    task_id = str(case.get("task_id") or "")\n    case_id = str(case.get("case_id") or "")\n    checkpoint = ((case.get("attempt") or {{}}).get("after") or {{}})\n    holder: dict[str, Any] = {{}}\n\n    def mutate(state):\n        task = state.setdefault("tasks", {{}}).setdefault(task_id, {{"total_attempts": 0, "runs": {{}}}})\n        retry_count = int(task.get("provider_retry_count") or 0)\n        exhausted = retry_count >= int(recovery.get("max_automatic_retries") or 0)\n        provider_entry = state.setdefault("providers", {{}}).setdefault(provider, {{}})\n        next_retry_at = int(\n            provider_entry.get("next_retry_at")\n            or _next_provider_retry_at(circuit, recovery)\n        )\n        wait = {{\n            "state": "HUMAN_REQUIRED" if exhausted else "WAITING",\n            "provider": provider,\n            "model": circuit.get("model"),\n            "case_id": case_id,\n            "board": _normalized_board(board),\n            "next_retry_at": None if exhausted else next_retry_at,\n            "retry_count": retry_count,\n            "max_automatic_retries": int(recovery.get("max_automatic_retries") or 0),\n            "checkpoint": checkpoint,\n            "created_at": _utc_now(),\n        }}\n        task["provider_wait"] = wait\n        task.pop("authorized_recovery_checkpoint", None)\n        index = (state.get("cases") or {{}}).get(case_id)\n        if isinstance(index, dict):\n            index["status"] = "HUMAN_REQUIRED" if exhausted else "WAITING_PROVIDER"\n        provider_entry["state"] = "OPEN"\n        provider_entry["human_reset_required"] = exhausted\n        provider_entry["next_retry_at"] = None if exhausted else next_retry_at\n        holder.update(wait=wait, exhausted=exhausted)\n\n    _update_state(mutate)\n    wait = holder["wait"]\n    if holder["exhausted"]:\n        status = "HUMAN_REQUIRED"\n        reason = (\n            f"Provider recovery exhausted for {{provider}} after {{wait['retry_count']}} automatic retries. "\n            f"Case {{case_id}} requires human intervention."\n        )\n        user_action = "true"\n    else:\n        status = "WAITING_PROVIDER"\n        reason = (\n            f"PROVIDER_WAIT provider={{provider}} case={{case_id}} retry_count={{wait['retry_count']}} "\n            f"next_retry_at={{wait['next_retry_at']}}. No human action required."\n        )\n        user_action = "false"\n\n    case["status"] = status\n    case.setdefault("provider_recovery", {{}}).update(wait)\n    _write_case(case)\n    held = _hold_task(\n        ctx, task_id, board, reason,\n        str(((policy.get("kanban_actions") or {{}}).get("hold_block_kind")) or "needs_input"),\n    )\n    _comment_task(\n        board, task_id,\n        "PROVIDER_WAIT\\n"\n        f"case_id: {{case_id}}\\nprovider: {{provider}}\\nstate: {{wait['state']}}\\n"\n        f"retry_count: {{wait['retry_count']}}/{{wait['max_automatic_retries']}}\\n"\n        f"next_retry_at: {{wait.get('next_retry_at')}}\\nuser_action_required: {{user_action}}\\n",\n    )\n    _append_event("provider_wait_armed", task_id=task_id, case_id=case_id, provider=provider, wait=wait, hold_result=held)\n    return {{"handled": True, "status": status, "wait": wait, "hold_result": held}}\n\n\ndef _revert_provider_resume(task_id: str, provider: str, case_id: str, resume_epoch: int) -> None:\n    def mutate(state):\n        task = (state.get("tasks") or {{}}).get(task_id)\n        if not isinstance(task, dict):\n            return\n        auth = task.get("authorized_recovery_checkpoint")\n        if isinstance(auth, dict) and int(auth.get("resume_epoch") or 0) == resume_epoch:\n            task.pop("authorized_recovery_checkpoint", None)\n        task["provider_retry_count"] = max(0, int(task.get("provider_retry_count") or 0) - 1)\n        wait = task.get("provider_wait")\n        if isinstance(wait, dict) and str(wait.get("case_id") or "") == case_id:\n            wait["state"] = "WAITING"\n        entry = (state.get("providers") or {{}}).get(provider)\n        if isinstance(entry, dict):\n            entry["state"] = "OPEN"\n            entry.pop("probe_task_id", None)\n            entry.pop("probe_resume_epoch", None)\n    _update_state(mutate)\n\n\ndef _resume_due_provider_waits(board_hint: str | None = None) -> None:\n    policy = _load_policy()\n    recovery = _provider_recovery_config(policy)\n    if not recovery.get("enabled"):\n        return\n    try:\n        with _file_lock(_state_path()):\n            snapshot = _read_state_unlocked()\n    except Exception as exc:\n        _append_event("provider_wait_scan_failed", error=str(exc)[:1000])\n        return\n\n    now_epoch = int(time.time())\n    candidates = []\n    for task_id, task in (snapshot.get("tasks") or {{}}).items():\n        if not isinstance(task, dict):\n            continue\n        wait = task.get("provider_wait")\n        if not isinstance(wait, dict) or wait.get("state") != "WAITING":\n            continue\n        due = int(wait.get("next_retry_at") or 0)\n        if due <= 0 or due > now_epoch:\n            continue\n        candidates.append((due, str(task_id), dict(wait), dict(task)))\n    candidates.sort(key=lambda item: item[0])\n    probed_providers: set[str] = set()\n\n    for _, task_id, wait_snapshot, task_snapshot in candidates:\n        provider = str(wait_snapshot.get("provider") or "")\n        if not provider:\n            continue\n        if recovery.get("one_probe_per_provider_per_dispatch_tick") and provider in probed_providers:\n            continue\n        provider_snapshot = (snapshot.get("providers") or {{}}).get(provider) or {{}}\n        if isinstance(provider_snapshot, dict) and provider_snapshot.get("state") == "PROBING":\n            continue\n        probed_providers.add(provider)\n        retry_count = int(task_snapshot.get("provider_retry_count") or 0)\n        if retry_count >= int(recovery.get("max_automatic_retries") or 0):\n            continue\n\n        holder: dict[str, Any] = {{}}\n        case_id = str(wait_snapshot.get("case_id") or "")\n        checkpoint = wait_snapshot.get("checkpoint") or {{}}\n\n        def mutate(state):\n            task = (state.get("tasks") or {{}}).get(task_id)\n            if not isinstance(task, dict):\n                return\n            wait = task.get("provider_wait")\n            if not isinstance(wait, dict) or wait.get("state") != "WAITING":\n                return\n            if str(wait.get("case_id") or "") != case_id:\n                return\n            if int(wait.get("next_retry_at") or 0) > int(time.time()):\n                return\n            current_count = int(task.get("provider_retry_count") or 0)\n            if current_count >= int(recovery.get("max_automatic_retries") or 0):\n                wait["state"] = "HUMAN_REQUIRED"\n                return\n            resume_epoch = int(task.get("resume_epoch") or 0) + 1\n            task["resume_epoch"] = resume_epoch\n            task["provider_retry_count"] = current_count + 1\n            dirty_checkpoint = (\n                isinstance(checkpoint, dict)\n                and checkpoint.get("available")\n                and int(checkpoint.get("changed_entries") or 0) > 0\n                and checkpoint.get("path")\n                and checkpoint.get("head")\n                and checkpoint.get("status_sha256")\n            )\n            if dirty_checkpoint:\n                task["authorized_recovery_checkpoint"] = {{\n                    "state": "AUTHORIZED",\n                    "reason": "provider_recovery",\n                    "case_id": case_id,\n                    "resume_epoch": resume_epoch,\n                    "workspace": checkpoint.get("path"),\n                    "head": checkpoint.get("head"),\n                    "status_sha256": checkpoint.get("status_sha256"),\n                    "authorized_at": int(time.time()),\n                    "expires_at": int(time.time()) + int(recovery.get("authorization_ttl_seconds") or 3600),\n                }}\n            wait["state"] = "RESUMING"\n            wait["resume_epoch"] = resume_epoch\n            wait["last_resume_at"] = _utc_now()\n            entry = state.setdefault("providers", {{}}).setdefault(provider, {{}})\n            entry["state"] = "PROBING"\n            entry["probe_task_id"] = task_id\n            entry["probe_resume_epoch"] = resume_epoch\n            entry["probe_started_at"] = _utc_now()\n            entry["human_reset_required"] = False\n            index = (state.get("cases") or {{}}).get(case_id)\n            if isinstance(index, dict):\n                index["status"] = "RETRY_AUTHORIZED"\n            holder.update(resume_epoch=resume_epoch, board=wait.get("board") or task.get("board_hint") or board_hint)\n\n        _update_state(mutate)\n        if not holder.get("resume_epoch"):\n            continue\n        board = _normalized_board(holder.get("board"))\n        resume_epoch = int(holder["resume_epoch"])\n\n        try:\n            from hermes_cli import kanban_db as kb\n            conn = kb.connect(board=board)\n            try:\n                task = kb.get_task(conn, task_id)\n                if task is None or getattr(task, "current_run_id", None) is not None:\n                    raise RuntimeError("task unavailable or still has current_run_id")\n                status = str(getattr(task, "status", "") or "")\n                if status == "blocked":\n                    if not kb.unblock_task(conn, task_id):\n                        raise RuntimeError("native unblock_task refused provider resume")\n                elif status != "ready":\n                    raise RuntimeError(f"task status {{status}} is not provider-resumable")\n                kb.add_comment(\n                    conn, task_id, author="execution-governance",\n                    body=(\n                        "PROVIDER_RETRY_AUTHORIZED\\n"\n                        f"case_id: {{case_id}}\\nprovider: {{provider}}\\nresume_epoch: {{resume_epoch}}\\n"\n                        f"automatic_retry: true\\nuser_action_required: false\\n"\n                    ),\n                )\n            finally:\n                conn.close()\n        except Exception as exc:\n            _revert_provider_resume(task_id, provider, case_id, resume_epoch)\n            _append_event("provider_wait_resume_failed", task_id=task_id, case_id=case_id, provider=provider, error=str(exc)[:1000])\n            continue\n\n        _update_case_status_file(case_id, "RETRY_AUTHORIZED", resume_epoch=resume_epoch, automatic_retry=True)\n        _append_event("provider_wait_resumed", task_id=task_id, case_id=case_id, provider=provider, resume_epoch=resume_epoch, board=board)\n\n\ndef _release_provider_probe(task_id: str, reason: str) -> None:\n    holder = []\n    def mutate(state):\n        for provider, entry in (state.get("providers") or {{}}).items():\n            if isinstance(entry, dict) and entry.get("state") == "PROBING" and str(entry.get("probe_task_id") or "") == task_id:\n                entry["state"] = "CLOSED"\n                entry["closed_at"] = _utc_now()\n                entry["close_reason"] = reason\n                entry.pop("probe_task_id", None)\n                entry.pop("probe_resume_epoch", None)\n                holder.append(provider)\n    try:\n        _update_state(mutate)\n    except Exception:\n        return\n    for provider in holder:\n        _append_event("provider_probe_closed", task_id=task_id, provider=provider, reason=reason)\n'''
    text = replace_once(text, helper_anchor, helper + helper_anchor, "governance provider wait helpers")

    # Route provider-budget cases into deterministic wait instead of the LLM Governor.
    generic_hold_anchor = '''        # Provider-budget failures and ordinary abnormal exits are both held.\n        # The Governor, not the original worker, decides whether a retry exists.\n        if (policy.get("task_budget") or {}).get("hold_on_first_unexpected_failure", True):\n'''
    provider_route = '''        if case.get("provider_circuit") and _provider_recovery_config(policy).get("enabled"):\n            handled = _arm_provider_wait(ctx, case, effective_board, policy)\n            if handled and handled.get("handled"):\n                return\n\n        # Any probe that ended for a non-budget reason proves that the provider\n        # itself is no longer the blocker; release the provider circuit and let\n        # ordinary governance classify the execution failure.\n        _release_provider_probe(tid, "worker_exit_without_provider_budget_error")\n\n        # Ordinary abnormal exits are held for the Execution Governor.\n        if (policy.get("task_budget") or {}).get("hold_on_first_unexpected_failure", True):\n'''
    text = replace_once(text, generic_hold_anchor, provider_route, "governance provider route")

    # Dispatch tick must wake due provider waits even when no timeout happened.
    factory_old = '''def _on_dispatch_tick_factory(ctx, worker_exited_handler):\n    \"\"\"Bridge native max-runtime outcomes into the governance exit pipeline.\n'''
    factory_new = '''def _on_dispatch_tick_factory(ctx, worker_exited_handler):\n    \"\"\"Bridge provider-wait wakeups and native max-runtime outcomes into governance.\n'''
    text = replace_once(text, factory_old, factory_new, "governance dispatch doc")
    text = replace_once(text, '''    del ctx\n\n    def on_dispatch_tick(result=None, board=None, **kwargs):\n        del kwargs\n        if result is None:\n            return\n''', '''    def on_dispatch_tick(result=None, board=None, **kwargs):\n        del kwargs\n        try:\n            _resume_due_provider_waits(board)\n        except Exception as exc:\n            _append_event("provider_wait_tick_failed", board=_normalized_board(board), error=str(exc)[:1000])\n        if result is None:\n            return\n''', "governance dispatch provider wake")

    # Successful task completion closes any active provider probe and clears stale authorization/wait.
    completed_old = '''def _kanban_task_completed(task_id=None, run_id=None, assignee=None, **kwargs):\n    del kwargs\n    if not task_id:\n        return\n    _append_event("task_completed", task_id=str(task_id), run_id=run_id, assignee=assignee)\n'''
    completed_new = '''def _kanban_task_completed(task_id=None, run_id=None, assignee=None, **kwargs):\n    del kwargs\n    if not task_id:\n        return\n    tid = str(task_id)\n    _release_provider_probe(tid, "task_completed")\n    try:\n        def mutate(state):\n            task = (state.get("tasks") or {}).get(tid)\n            if isinstance(task, dict):\n                task.pop("authorized_recovery_checkpoint", None)\n                task.pop("provider_wait", None)\n        _update_state(mutate)\n    except Exception:\n        pass\n    _append_event("task_completed", task_id=tid, run_id=run_id, assignee=assignee)\n'''
    text = replace_once(text, completed_old, completed_new, "governance completion cleanup")

    # Mark authorization as IN_USE on actual spawn so it cannot authorize a second respawn.
    spawn_anchor = '''        runs[rid] = {\n            **(runs.get(rid) or {}), "run_id": run_id, "profile": profile,\n            "board": effective_board, "tenant": tenant, "worker_pid": worker_pid,\n            "workspace_path": workspace_path, "started_at": _utc_now(), "before": before,\n            "tool_calls": int((runs.get(rid) or {}).get("tool_calls") or 0),\n            "tool_counts": dict((runs.get(rid) or {}).get("tool_counts") or {}),\n        }\n        holder.update(board=effective_board, tenant=tenant, scope_status=scope.get("scope_status"))\n'''
    spawn_new = '''        runs[rid] = {\n            **(runs.get(rid) or {}), "run_id": run_id, "profile": profile,\n            "board": effective_board, "tenant": tenant, "worker_pid": worker_pid,\n            "workspace_path": workspace_path, "started_at": _utc_now(), "before": before,\n            "tool_calls": int((runs.get(rid) or {}).get("tool_calls") or 0),\n            "tool_counts": dict((runs.get(rid) or {}).get("tool_counts") or {}),\n        }\n        authorization = task.get("authorized_recovery_checkpoint")\n        if isinstance(authorization, dict) and authorization.get("state") == "AUTHORIZED":\n            authorization["state"] = "IN_USE"\n            authorization["run_id"] = rid\n            authorization["claimed_at"] = _utc_now()\n            runs[rid]["resume_epoch"] = authorization.get("resume_epoch")\n            wait = task.get("provider_wait")\n            if isinstance(wait, dict) and wait.get("state") == "RESUMING":\n                wait["state"] = "RUNNING"\n                wait["run_id"] = rid\n        holder.update(board=effective_board, tenant=tenant, scope_status=scope.get("scope_status"))\n'''
    text = replace_once(text, spawn_anchor, spawn_new, "governance spawn authorization claim")
    return text


def patch_plugin_version(text: str, version: str) -> str:
    return re.sub(r"(?m)^version:\s*[^\n]+$", f"version: {version}", text, count=1)


def paths_for(root: Path, mode: str) -> dict[str, Path | list[Path]]:
    if mode == "backup":
        return {
            "architect_soul": root / "implementation-architect_SOUL.md",
            "orchestrator_soul": root / "implementation-orchestrator_SOUL.md",
            "worker_soul": root / "implementation-worker_SOUL.md",
            "governor_soul": root / "execution-governor_SOUL.md",
            "architect_config": root / "implementation-architect_config.yaml",
            "orchestrator_config": root / "implementation-orchestrator_config.yaml",
            "worker_config": root / "implementation-worker_config.yaml",
            "policy": root / "execution-governor" / "governance" / "policy.yaml",
            "skills": [root / "skills" / "godot-development" / "SKILL.md"],
            "plugins": root / "plugins",
        }
    profiles = root / "profiles"
    skills = []
    for profile in ("implementation-worker", "implementation-architect", "implementation-orchestrator"):
        candidate = profiles / profile / "skills" / "godot-development" / "SKILL.md"
        if candidate.exists():
            skills.append(candidate)
    return {
        "architect_soul": profiles / "implementation-architect" / "SOUL.md",
        "orchestrator_soul": profiles / "implementation-orchestrator" / "SOUL.md",
        "worker_soul": profiles / "implementation-worker" / "SOUL.md",
        "governor_soul": profiles / "execution-governor" / "SOUL.md",
        "architect_config": profiles / "implementation-architect" / "config.yaml",
        "orchestrator_config": profiles / "implementation-orchestrator" / "config.yaml",
        "worker_config": profiles / "implementation-worker" / "config.yaml",
        "policy": profiles / "execution-governor" / "governance" / "policy.yaml",
        "skills": skills,
        "plugins": root / "plugins",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply Hermes operational hardening without touching any remote Git repository.")
    parser.add_argument("--root", required=True, help="Backup repository root or live /opt/data root")
    parser.add_argument("--mode", choices=("backup", "live"), required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    root = Path(args.root).resolve()
    mapping = paths_for(root, args.mode)
    plugins = Path(mapping["plugins"])

    targets: list[tuple[Path, Callable[[str], str] | None, str | None]] = [
        (require(Path(mapping["architect_soul"])), patch_architect_soul, None),
        (require(Path(mapping["orchestrator_soul"])), patch_orchestrator_soul, None),
        (require(Path(mapping["worker_soul"])), patch_worker_soul, None),
        (require(Path(mapping["governor_soul"])), None, execution_governor_soul()),
        (require(Path(mapping["architect_config"])), patch_config_remove_native_computer, None),
        (require(Path(mapping["orchestrator_config"])), patch_config_remove_native_computer, None),
        (require(Path(mapping["worker_config"])), patch_worker_config, None),
        (require(Path(mapping["policy"])), None, policy_yaml()),
        (require(plugins / "governance-guard" / "__init__.py"), patch_governance_guard, None),
        (require(plugins / "execution-governance" / "__init__.py"), patch_execution_governance, None),
        (require(plugins / "retry-checkpoint-guard" / "__init__.py"), patch_retry_checkpoint_guard, None),
        (require(plugins / "worktree-guardian" / "__init__.py"), patch_worktree_guardian, None),
        (require(plugins / "governance-guard" / "plugin.yaml"), lambda s: patch_plugin_version(s, "0.3.0"), None),
        (require(plugins / "execution-governance" / "plugin.yaml"), lambda s: patch_plugin_version(s, "0.3.0"), None),
        (require(plugins / "retry-checkpoint-guard" / "plugin.yaml"), lambda s: patch_plugin_version(s, '"1.1.0"'), None),
        (require(plugins / "worktree-guardian" / "plugin.yaml"), lambda s: patch_plugin_version(s, "1.1.0"), None),
    ]
    for skill_path in mapping["skills"]:
        targets.append((require(Path(skill_path)), None, godot_skill()))

    changed: list[tuple[Path, str, str]] = []
    for path, transform, replacement in targets:
        before = read_text(path)
        after = replacement if replacement is not None else transform(before)  # type: ignore[misc]
        if after != before:
            changed.append((path, before, after))

    print(f"Root: {root}")
    print(f"Mode: {args.mode}")
    print(f"Files requiring changes: {len(changed)}")
    for path, before, after in changed:
        print(f"  {path.relative_to(root)}  {hashlib.sha256(before.encode()).hexdigest()[:10]} -> {hashlib.sha256(after.encode()).hexdigest()[:10]}")

    if args.dry_run:
        print("DRY RUN: no files written.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_root = root / f".operational-hardening-backup-{stamp}"
    for path, before, after in changed:
        rel = path.relative_to(root)
        backup = backup_root / rel
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backup)
        write_text(path, after)

    manifest = {
        "marker": MARKER,
        "applied_at": datetime.now().isoformat(),
        "mode": args.mode,
        "root": str(root),
        "backup_root": str(backup_root),
        "changed_files": [str(path.relative_to(root)) for path, _, _ in changed],
    }
    (backup_root / "PATCH_MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Applied successfully. Backup: {backup_root}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f"PATCH ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
