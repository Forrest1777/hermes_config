# gwrm-gut-runner 0.1.2

Plugin Hermes para reduzir turnos/tokens desperdiçados durante GUT long-running via GWRM.

## O que mudou na 0.1.1

A 0.1.0 podia esperar 720–1800 s dentro de uma única tool call, mas no ambiente Hermes observado a chamada externa é encerrada por volta de 420 s. Isso podia deixar uma operação GUT ainda ativa e induzir novas chamadas/retries.

A 0.1.1 corrige isso:

- janela segura de espera: default/effective máximo de 360 s com os defaults atuais;
- `max_wait_seconds` acima da janela segura continua aceito por compatibilidade, mas é clampado;
- nova tool `gwrm_gut_wait_existing(operation_id)`, que **nunca inicia GUT**;
- `WAIT_WINDOW_EXPIRED` é retorno `ok=true`, `terminal=false`, não falha operacional;
- proteção process-local contra waiters simultâneos do mesmo `operation_id`;
- cancelamento remove o waiter do registro local, sem matar o GUT (GWRM continua owner);
- cache persistente de falha terminal por `worktree + selection + Git state`;
- repetição idêntica devolve `REPEATED_TERMINAL_RESULT` sem iniciar novo GUT;
- `force_rerun=true` existe para rerun deliberado;
- em falha persistente de status, um único health probe classifica `GWRM_UNAVAILABLE` vs `STATUS_UNAVAILABLE`.

## Fluxo recomendado

```text
LLM
  -> gwrm_gut_run_and_wait
       -> start/reuse GUT
       -> polling interno <= safe window
       -> terminal OU WAIT_WINDOW_EXPIRED

se terminal=false:
  -> gwrm_gut_wait_existing(operation_id)
       -> polling interno <= safe window
       -> terminal OU novo WAIT_WINDOW_EXPIRED
```

Nunca use `gwrm_gut_run_and_wait` para "continuar esperando" uma operação cujo `operation_id` já é conhecido.

## Semântica principal

- `ok=true, reason=TESTS_PASSED, terminal=true, passed=true`
- `ok=true, reason=TESTS_FAILED, terminal=true, passed=false`
- `ok=true, reason=WAIT_WINDOW_EXPIRED, terminal=false`
- `ok=true, reason=REPEATED_TERMINAL_RESULT, terminal=true, rerun_started=false`
- `ok=true, reason=WAITER_ALREADY_ACTIVE, terminal=false`
- `ok=false, reason=GWRM_UNAVAILABLE, terminal=false`
- `ok=false, reason=STATUS_UNAVAILABLE, terminal=false`

## Configuração

```yaml
gwrm_gut_runner:
  poll_interval_seconds: 5
  max_wait_seconds: 360
  external_tool_timeout_seconds: 420
  tool_timeout_safety_margin_seconds: 60
  http_timeout_seconds: 10
  max_failure_output_chars: 3000
  max_consecutive_status_errors: 3
```

Com esses defaults:

```text
safe cap = 420 - 60 = 360 s
```

Uma chamada que pedir `max_wait_seconds: 900` terá:

```text
requested_wait_seconds = 900
effective_wait_seconds = 360
```

## Toolset

```yaml
platform_toolsets:
  cli:
    - gwrm_gut_runner

plugins:
  enabled:
    - gwrm-gut-runner

known_plugin_toolsets:
  cli:
    - gwrm_gut_runner
```

## Observabilidade

Audit:

```text
/opt/data/logs/gwrm-gut-runner/events.jsonl
```

Cache de falhas terminais:

```text
/opt/data/logs/gwrm-gut-runner/terminal-cache-v1.json
```

O cache não contém API key nem stdout/stderr completos.

## Segurança / lifecycle

O plugin não:

- altera Kanban;
- faz commit/push;
- deativa worktree;
- mata o processo GUT quando a tool é cancelada;
- aceita gate em nome do agente.

O GWRM continua owner da operação GUT.


## Correção 0.1.2 — falha terminal não verificada

A 0.1.1 ainda podia interpretar `exit_code=1` como `TESTS_FAILED` mesmo quando o GUT não
produzia JUnit, as contagens estavam nulas e não havia falha estruturada.

A 0.1.2 distingue:

- `TESTS_FAILED`: há evidência estruturada de teste (JUnit, contagens parseadas, failure ou fatal pattern);
- `GUT_RESULT_UNVERIFIED`: processo terminou vermelho, mas não há evidência suficiente para dizer que
  uma assertion/teste falhou.

`GUT_RESULT_UNVERIFIED` é `ok=false`, não entra no cache de falhas terminais e deve ser tratado como
problema de execução/produção de resultado.

O cache passa a usar `terminal-cache-v2.json`, ignorando automaticamente entradas produzidas pela
semântica ampla da 0.1.1.
