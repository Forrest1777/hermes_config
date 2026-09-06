# Test plan

## Static
- APPLY dry-run reports exactly 4 files.
- VERIFY passes.
- restart Hermes.

## Degraded recovery robustness test (existing state)
Preserve:
- root `t_ef479657` in TODO
- worker `t_b10a9a99` in TRIAGE
- existing dirty worktree and WIP

Expected after restart/catch-up:
1. catch-up identifies durable retry-checkpoint-guard evidence for `t_b10a9a99`.
2. a deterministic `dirty-recovery-t_b10a9a99-<fingerprint>` case is created/reused.
3. stale ERROR Governor is reconciled only if safely inactive.
4. historical PENDING cases >=24h become STALE, preserved.
5. a fresh/reusable Execution Governor singleton services the current case.
6. only RETRY_AUTHORIZED may create exact authorization + resume_epoch and requeue TRIAGE → READY.
7. same WIP survives.

This degraded test is robustness evidence only; it does NOT make Gate E PASS.

## New canonical Gate E
Start a new root/worker/worktree after v2 is healthy. Required canonical sequence:
clean run → intentional dirty checkpoint → blind redispatch blocked → event-driven case → Execution Governor → exact authorization → same worktree resumed → WIP preserved → commit → clean → stale-fingerprint negative test.

Only this new end-to-end run can mark Gate E PASS.
