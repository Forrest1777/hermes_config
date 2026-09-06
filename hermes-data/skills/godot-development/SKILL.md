---
name: godot-development
description: Desenvolvimento e verificacao de worktrees Godot/GDScript usando GWRM, LSP e GUT.
---

# Godot/GDScript no Hermes com GWRM

## Inicio e encerramento

1. Valide a worktree já provisionada pelo dispatcher com a skill `worktree-preflight`.
2. Resolva `worktree_name` a partir do basename de `HERMES_KANBAN_WORKSPACE`.
3. Chame `activate_worktree` e aguarde `status: ready` antes de trabalhar com GDScript.
4. Use sempre a mesma `worktree_name` nas tools Godot e GUT.
5. Chame `deactivate_worktree` antes de concluir ou devolver o card e confirme `stopped`, sem PIDs residuais.

O worker nao inicia Godot, nao escolhe portas e nao converte caminhos manualmente.

## Caminhos

- Use `res://` para recursos do projeto.
- Use `user://` para dados gravaveis.
- Nao grave caminhos absolutos de Windows ou Linux no codigo, cenas ou recursos.
- Caminhos absolutos pertencem apenas a configuracao externa do GWRM.

## Validacao

1. Edite os arquivos da worktree associada ao card.
2. Leia e corrija os diagnosticos LSP retornados pelo Hermes.
3. Use `godot_lsp_status` ou `get_worktree_status` quando houver falha de conexao.
4. Execute GUT no mesmo `worktree_name`.
5. Nao trate ausencia de diagnosticos como prova de correcao de runtime.
6. Nao considere GUT aprovado sem scripts, testes e assertagens maiores que zero e sem falhas/erros.

## Separacao

- LSP: sintaxe, tipos, simbolos, referencias e diagnosticos semanticos.
- Godot MCP dedicado: cenas, nos, recursos, execucao e logs da worktree.
- GUT: comportamento e integracao no projeto da worktree.
- GWRM: lifecycle, importacao, portas, processos, mapeamento e cleanup.
