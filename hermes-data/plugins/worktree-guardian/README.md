# worktree-guardian 1.0.1

Plugin único de proteção do ciclo de vida de worktrees para os profiles de implementação.

## Mudança de segurança 1.0.1

Hermes v0.20.x pode auto-promover um card criado diretamente com `initial_status: blocked`. Por isso, o Guardian não usa mais o status `blocked` como barreira primária. A barreira é um **assignee não-spawnável** configurável (`__worktree_guardian_hold__`).

Fluxo:

1. O orquestrador cria o card com `assignee: __worktree_guardian_hold__`, `workspace_kind: worktree`, `goal_mode: false` e `base_ref` explícito.
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
