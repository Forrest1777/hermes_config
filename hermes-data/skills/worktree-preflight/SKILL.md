---
name: worktree-preflight
description: Valida integralmente uma worktree Kanban já provisionada pelo dispatcher antes de ativar o GWRM ou editar o projeto.
---

# Worktree Preflight

## Quando usar

Execute no início de todo card que possa ler tecnicamente, editar, testar ou validar arquivos em uma worktree. O dispatcher já deve ter materializado o workspace. Esta skill nunca cria, repara, move ou remove worktrees.

## Invariantes

- `HERMES_KANBAN_WORKSPACE` é obrigatório e autoritativo.
- A worktree deve estar sob `/workspace`.
- O toplevel deve ser exatamente o path resolvido do workspace.
- A worktree deve compartilhar o Git common dir do repositório esperado.
- Branch e `HEAD` devem corresponder ao card e ao `base_ref`.
- O estado inicial deve estar limpo.
- Sparse checkout, sparse index e `SKIP_WORKTREE` são proibidos.
- Todo arquivo retornado por `git ls-files` deve existir fisicamente.
- `project.godot` e `AGENTS.md` devem existir.
- Falha produz `BLOCKED_OPERATIONAL`; não tente corrigir o workspace.

## Execução

1. Leia o card e obtenha branch/base esperadas.
2. Execute `scripts/worktree-preflight.sh`, passando branch e base quando disponíveis:

```bash
./scripts/worktree-preflight.sh \
  --workspace "$HERMES_KANBAN_WORKSPACE" \
  --expected-branch "${HERMES_KANBAN_BRANCH:-}" \
  --base-ref "<base_ref_do_card>"
```

3. Capture a saída JSON.
4. Prossiga para `activate_worktree` somente quando `passed` for `true`.

## Falha

Bloqueie o card incluindo pelo menos: `workspace`, branch esperada/encontrada, `head`, `base_ref`, `git_common_dir`, flags sparse, entradas skip-worktree, primeiros arquivos ausentes e erros. Não execute `git worktree add`, `git sparse-checkout disable`, reset, checkout, prune ou remoção.

## Observação sobre RTK

O script usa `command git` para comandos de plumbing e saída NUL/machine-readable. Para revisão humana de `status`, `diff` e `log`, continue usando RTK conforme `AGENTS.md`.
