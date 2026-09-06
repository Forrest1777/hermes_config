# Test plan

## Static gate
1. APPLY dry-run reports exactly the intended 6 files.
2. APPLY succeeds and creates timestamped backup.
3. VERIFY returns PASS.
4. Restart Hermes; no plugin-load error.

## Current Gate E continuation
1. Preserve current worker worktree exactly.
2. Resume the existing orchestrator root only.
3. Orchestrator calls `request_dirty_checkpoint_recovery` for the existing worker.
4. Request result must show:
   - `action=REQUEST_DIRTY_CHECKPOINT_RECOVERY`
   - `retry_authorized=false`
   - `target_reactivated=false`
   - a PENDING case id
5. Persistent Execution Governor runs.
6. For current preserved checkpoint, expected decision is RETRY_AUTHORIZED if attempts/circuit/policy remain valid.
7. execution-governance revalidates exact live fingerprint.
8. Since current worker is in triage from block-loop detection, only the narrow governed triage->ready CAS may requeue it.
9. retry-checkpoint-guard allows exact authorization.
10. worktree-guardian reports authorized retry + resume_epoch.
11. Same worker card/worktree resumes; original WIP remains.
12. Worker finishes phase B and commits; no push/integration.

## Negative fingerprint test
After obtaining a fresh authorized dirty checkpoint, mutate the worktree before dispatch. Expected:
- exact fingerprint no longer matches;
- retry-checkpoint-guard/worktree-guardian reject;
- no worker spawn on the mismatched authorization;
- no reset/clean/delete.
