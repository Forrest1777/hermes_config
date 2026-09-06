# Hermes Dirty Checkpoint Recovery v2.1 — 2026-09-04

This package extends the already-applied Dirty Checkpoint Recovery v1.

## Why v2 exists

The first live Gate E smoke exposed two independent control-plane gaps:

1. A dirty retry could be blocked correctly by `retry-checkpoint-guard`, but recovery request depended on an orchestrator root that might itself be parent-gated in `todo`.
2. The persistent Execution Governor board state could remain `ERROR` with a stale/missing Governor task and historical `PENDING` cases, preventing a newly valid recovery case from being serviced.

## v2 behavior

### Canonical event-driven request

A durable `kanban_task_blocked` reason beginning with `RETRY_CHECKPOINT_GUARD:` causes `governance-guard` to create/reuse a deterministic `DIRTY_CHECKPOINT_RECOVERY` case. This is a **request only**. It never grants authorization and never reactivates the worker.

`request_dirty_checkpoint_recovery` remains as an orchestrator fallback/proactive tool, but is no longer required for the normal flow.

### Restart catch-up

Every bounded catch-up interval, the dispatcher scans up to the configured number of blocked/triage implementation cards and looks for the durable `retry-checkpoint-guard` comment. This lets a pre-v2 checkpoint (including the current degraded smoke) enter governance after restart without forcing its root READY.

### Stale Execution Governor reconciliation

Only when board lifecycle is `ERROR`, v2 checks the mapped Governor task. It refuses reconciliation if the Governor may still be active or if a recent active case exists. When the mapped task is missing (or safely done), old `PENDING` cases older than the configured TTL are marked `STALE` and preserved on disk. No case file is deleted.

Default TTL: 24 hours.

After stale quarantine, a missing singleton mapping can be rebuilt and a new Governor singleton created for current work.

## Safety invariants

- no Hermes/Kanban core modification
- no Git reset/clean/delete
- no direct dirty-checkpoint authorization from the request path
- exact live fingerprint is still revalidated by execution-governance v1 before `RETRY_AUTHORIZED`
- `resume_epoch` remains owned by execution-governance
- recent/active/unreadable governance cases fail closed
- stale cases are preserved, not deleted
- no push or repository mutation

## Apply sequence

1. dry-run
2. apply
3. verify
4. restart Hermes
5. observe current degraded smoke as a robustness test
6. regardless of degraded result, create a NEW Gate E from the beginning for canonical end-to-end validation


## v2.1 packaging correction

The first v2 dry-run failed closed before writing because its patcher searched for the pre-hardening `on_dispatch_tick` shape. The live 2026-09-03 hardening had already inserted `_resume_due_provider_waits(board)` there. v2.1 changes only that patch anchor and preserves the intended v2 runtime behavior and target version (`governance-guard` 0.3.2).
