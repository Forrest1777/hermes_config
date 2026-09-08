# worktree-guardian 1.4.0

## Change 1.4.0 - governed exhausted-recovery base

Normal child cards still derive their base from the canonical root `integration_head`. A recovery replacement may use a different `base_ref` only when the card carries the exact `GOVERNANCE_RECOVERY_REPLACEMENT_AUTHORIZED` comment written by governance-guard, the old card is archived as `SUPERSEDED_RECOVERY`, the target profile matches, and the recovery commit descends from the canonical `integration_head`. Every mismatch fails closed.

Plugin único de proteção do ciclo de vida de worktrees para os profiles de implementação.

## Mudança 1.3.0 — integration_head autoritativo

Quando o root ativo possui um `OPERATIONAL_CHECKPOINT_CANONICAL`, `worktree_guardian_prepare` não depende mais do HEAD do checkout âncora do dispatcher:

1. lê o checkpoint canônico mais recente do root;
2. resolve `operational_checkpoint.integration_head` no repositório comum;
3. exige que esse commit seja exatamente o `HEAD` da worktree ativa do root e, quando informado, que `integration_target_branch` corresponda à branch ativa;
4. trata `base_ref` passado à tool apenas como assertion opcional;
5. reconcilia `base_ref` e `created_from_commit` no child para o commit autoritativo;
6. cria/valida a worktree do child exatamente desse commit antes de liberar o assignee real.

Assim, `anchor HEAD != integration_head` é suportado desde que ambos pertençam ao mesmo Git common-dir e o commit do integration head seja resolvível. Divergência checkpoint↔root HEAD falha fechada. Roots legados sem checkpoint mantêm o fluxo explícito antigo.


## Mudança de segurança 1.0.1

Hermes v0.20.x pode auto-promover um card criado diretamente com `initial_status: blocked`. Por isso, o Guardian não usa mais o status `blocked` como barreira primária. A barreira é um **assignee não-spawnável** configurável (`__worktree_guardian_hold__`).

Fluxo:

1. O orquestrador cria o card com `assignee: __worktree_guardian_hold__`, `workspace_kind: worktree`, `goal_mode: false`; em roots checkpointed o Guardian deriva a base do `integration_head`.
2. Mesmo que o dispatcher coloque o card em `ready`, ele não consegue spawnar porque o assignee não resolve para um profile.
3. `worktree_guardian_prepare(target_task_id, target_profile, ...)` provisiona e valida a worktree.
4. Só depois de PASS o Guardian usa a mutação Kanban nativa para atribuir `target_profile` (`implementation-worker` ou `implementation-architect`).
5. A partir desse momento o dispatcher pode spawnar com a worktree já validada.
6. Se o prepare falhar, o card permanece com o holding assignee e `human_required=true`.

## Tools

- `worktree_guardian_prepare` — somente `implementation-orchestrator`. Prepara a worktree atrás do assignee fence e ativa o assignee real somente após validação.
- `worktree_guardian_verify` — somente `implementation-worker` e `implementation-architect`. Verificação fail-closed no início de cada run. Nunca repara a worktree.
- `worktree_guardian_recover_index_lock` — somente `implementation-orchestrator`. Recuperação conservadora do `index.lock` exato resolvido pelo Git quando zero-byte, stale, não-symlink e sem processo Git associado.

## Invariantes

- O Guardian falha se `holding_assignee` resolver para um profile real.
- Antes do PASS, o card deve continuar atribuído exatamente ao holding assignee e não pode possuir qualquer run anterior.
- Status `ready`, `blocked` ou `todo` pode mudar por mecanismos nativos sem quebrar o gate: **o assignee fence é a autoridade de dispatch**.
- O assignee real só é aplicado depois de `validation.passed=true`.
- Worker/architect ainda executam `verify` em todo run como segunda barreira independente.
- Cleanup de provisionamento é exclusivamente Git-aware; diretório não registrado nunca é apagado por filesystem.
- Branch já existente nunca é resetada/deletada automaticamente.
- Timeout de `git worktree add` encerra o grupo de processos.

## Configuração

```yaml
worktree_guardian:
  workspace_root: /workspace
  holding_assignee: __worktree_guardian_hold__
  required_files:
    - project.godot
    - AGENTS.md
  validation_timeout_seconds: 60
  provisioning:
    max_attempts: 3
    timeout_initial_seconds: 120
    timeout_increment_seconds: 90
    timeout_max_seconds: 300
    recovery_command_timeout_seconds: 90
    process_settle_seconds: 1
  stale_index_lock:
    min_age_seconds: 120
```
