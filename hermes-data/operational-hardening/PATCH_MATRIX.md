# Hermes Operational Hardening — 2026-09-03

## Scope implemented by this patch

| Problem | Change | Files/components |
|---|---|---|
| Architect approval deadlock | Approval becomes the only normal human gate. After human approval + READY, architect derives deterministic fields from its own approved bundle and continues to READY_FOR_INTEGRATION. | `implementation-architect_SOUL.md`, `implementation-orchestrator_SOUL.md` |
| ORCHESTRATOR_MATERIALIZATION handshake | Removed from the normal architecture path. `orchestration_unblock_card` remains available only as exceptional operational recovery; plugin code is not broadened in this patch. | SOULs only |
| Provider budget/quota exhaustion | Converts provider quota into deterministic `WAITING_PROVIDER`; wakes one probe per provider after configurable X minutes; retries do not use a sleeping worker/session. | `governance-guard`, governor policy/SOUL |
| Configurable retry interval | `provider_recovery.retry_interval_minutes`, default 20. | `execution-governor/governance/policy.yaml` |
| Provider retry safety | One probe/provider/tick, default max 12 automatic retries, 60-min checkpoint authorization TTL. | policy + `governance-guard` |
| Dirty retry checkpoint | Governance authorizes an exact `{workspace, HEAD, git-status SHA256}` fingerprint; blind dirty redispatch stays blocked. | `execution-governance`, `retry-checkpoint-guard`, `worktree-guardian` |
| Resume epoch | Each authorized retry gets a monotonically increasing governance `resume_epoch`, persisted/audited and included in retry comments. | `execution-governance`, `governance-guard` |
| Worktree Guardian conflict with legitimate WIP | `worktree_guardian_verify` accepts dirty state only when exact governance authorization matches; all other dirty states still fail closed. | `worktree-guardian` |
| Retry checkpoint guard conflict | Pre-spawn guard allows only an exact `AUTHORIZED` fingerprint; authorization becomes `IN_USE` when the worker actually spawns, preventing blind second respawn. | `retry-checkpoint-guard`, `governance-guard` |
| Excessive full-suite runs | Microchanges => focused/impacted tests; subsystem suite when justified; full suite remains mandatory at consolidated final gate. | worker/orchestrator SOUL, `godot-development` skill |
| Wrong Computer Use backend | Implementation profiles lose active native `computer_use`; worker gets GWRM `gui_*` MCP allowlist and explicit GWRM-only GUI policy. | three implementation configs, worker SOUL, `godot-development` skill |
| Re-reading / token churn | Fast-resume/handoff discipline: reference durable IDs/paths/commits instead of re-pasting large evidence. | orchestrator SOUL |

## Intentionally unchanged

- Core Hermes/Kanban code.
- `operational-block-completion-guard`: current OP-09 behavior remains correct and fail-closed.
- `gwrm-gut-runner`: existing supervised wait/no-poll policy remains.
- GWRM half-alive: monitor; investigate only if reproduced after GWRM 1.1.0 fixes.
- `.gd.uid`: already resolved by user in `skill_system_framework/main`.
- `ai-arena-stage04-behavioral-baseline.json`: project cleanup is a separate repository/card change and is not mixed into Hermes operational infrastructure.
- Provider fallback to another provider/model: not enabled. This patch retries the configured provider after the wait interval.

## Provider recovery defaults

```yaml
provider_recovery:
  enabled: true
  retry_interval_minutes: 20
  max_automatic_retries: 12
  prefer_provider_reset_time: false
  authorization_ttl_minutes: 60
  one_probe_per_provider_per_dispatch_tick: true
```

Set `prefer_provider_reset_time: true` if you later prefer waiting until a provider-supplied `resets_at` instead of probing every X minutes.
