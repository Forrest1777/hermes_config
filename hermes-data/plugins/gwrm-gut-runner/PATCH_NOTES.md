# PATCH NOTES — 0.1.2

Correção baseada no incidente real do card t_2033345c.

## Defeitos corrigidos

1. `max_wait_seconds` maior que o timeout externo do Hermes.
2. Reentrada em `gwrm_gut_run_and_wait` para a mesma operação ativa.
3. Waiters concorrentes para o mesmo `operation_id`.
4. Reruns idênticos após um resultado terminal vermelho sem mudança no Git.
5. Falha de status sem classificação simples de saúde do GWRM.

## Política recomendada para SOUL

Use `gwrm_gut_run_and_wait` apenas para iniciar/reusar a seleção.

Se retornar `terminal=false` com `reason=WAIT_WINDOW_EXPIRED`, continue exclusivamente com
`gwrm_gut_wait_existing(operation_id)`.

Se retornar `reason=REPEATED_TERMINAL_RESULT`, não rerode. Faça progresso diagnóstico/alteração
de estado. Use `force_rerun=true` somente com justificativa explícita.

Se retornar `reason=GWRM_UNAVAILABLE`, preserve WIP e bloqueie operacionalmente; não faça polling
manual nem tente novos GUTs.


## 0.1.2

- não classifica `exit_code=1` + JUnit ausente + counts nulos como `TESTS_FAILED`;
- novo `reason=GUT_RESULT_UNVERIFIED`;
- resultados não verificados não alimentam dedupe/cache;
- cache novo `terminal-cache-v2.json` invalida automaticamente entradas da 0.1.1.
