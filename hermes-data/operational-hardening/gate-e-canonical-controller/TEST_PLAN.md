# Gate E Canonical Pass Contract

## E1 Positive

PASS only if:
1. fresh task starts from clean worktree;
2. checkpoint A is the only dirty path;
3. blind redispatch produces retry-checkpoint guard evidence;
4. no worker spawn occurs between guard refusal and governance requeue;
5. event-driven recovery reaches real Execution Governor `RETRY_AUTHORIZED`;
6. `resume_epoch > 0`;
7. same worktree resumes;
8. `ORIGINAL_WIP_A_MUST_SURVIVE` is preserved;
9. worker records `worktree_guardian_authorized: true`;
10. worker completes with a clean worktree;
11. no push/integration.

## E2 Negative

PASS only if:
1. separate fresh worker has exactly one setup spawn;
2. checkpoint B is dirty and is captured by a deterministic recovery case;
3. Governor wake-up is held only in the controller process;
4. B -> C mutation keeps workspace and HEAD unchanged but changes `status_sha256`;
5. production `execution-governance.governance_decide(RETRY_AUTHORIZED)` rejects specifically because the fingerprint changed;
6. no `authorized_recovery_checkpoint` exists after rejection;
7. worker remains blocked/triage and spawn count remains exactly one;
8. held case is terminalized via normal `HUMAN_REQUIRED` decision;
9. negative dirty worktree is preserved as evidence;
10. no push/integration.

## Gate E

`GATE_E_CANONICAL: PASS` only if E1 PASS and E2 PASS.
Any divergence is FAIL CLOSED; do not manually unblock/repair the generated workers while the controller is running.


## v1.1.0 canonical negative probe

E2 now validates the v2.1 production contract exactly: checkpoint identity is SHA-256 of `git status --porcelain=v1 --untracked-files=all`. Checkpoint B contains one dirty path; controlled checkpoint C adds a second allowed untracked probe path before the production `RETRY_AUTHORIZED` boundary is invoked. This guarantees `status_sha256_B != status_sha256_C` without reset/clean/index mutation and proves stale-status fingerprint rejection. Content-only mutation of an already-dirty path is intentionally out of scope for v2.1.
