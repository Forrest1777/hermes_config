# Patch matrix

| Component | Change | Authority impact |
|---|---|---|
| governance-guard | Adds `request_dirty_checkpoint_recovery` to existing `orchestration_control` toolset | Request-only; orchestrator cannot authorize/requeue |
| governance-guard | Validates root-child relationship, target profile/status/current run, and dirty Git checkpoint | Fail-closed |
| governance-guard | Creates idempotent `DIRTY_CHECKPOINT_RECOVERY` case and queues persistent Governor | No worker lifecycle mutation |
| execution-governance | Exposes recovery facts in `governance_context` | Read-only context |
| execution-governance | Revalidates live workspace/HEAD/status SHA256 before authorization | Stronger fail-closed invariant |
| execution-governance | Allows `triage -> ready` only for Governor-authorized `DIRTY_CHECKPOINT_RECOVERY` with exact checkpoint authorization | Narrow recovery authority owned by Governor |
| implementation-orchestrator SOUL | Routes dirty retry blocks to `request_dirty_checkpoint_recovery`, not another cross-card unblock | Reduces bypass risk |
| execution-governor SOUL | Allows validated dirty checkpoint recovery as a legitimate RETRY_AUTHORIZED case | Decision still bounded by deterministic plugin checks |

No change:
- retry-checkpoint-guard exact fingerprint enforcement
- worktree-guardian exact fingerprint enforcement
- provider recovery flow
- core Hermes block-loop detector
- Kanban core
