---
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

## GUI Windows / Computer Use (HERMES_OPERATIONAL_HARDENING_2026_09_03)

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
