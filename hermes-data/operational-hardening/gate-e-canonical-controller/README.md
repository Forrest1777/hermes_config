# Gate E Canonical — Deterministic Controller

This package replaces the failed LLM-root orchestration topology for Gate E.
It does **not** patch Hermes or the operational plugins. It uses the already-installed Dirty Checkpoint Recovery v2.1.

## Canonical design

Gate E is now two fresh, isolated subtests controlled by one deterministic process:

- **E1 Positive end-to-end recovery**
  - fresh implementation-worker
  - clean worktree
  - checkpoint A dirty
  - one blind redispatch
  - retry-checkpoint-guard fail-closed
  - event-driven DIRTY_CHECKPOINT_RECOVERY
  - real Execution Governor RETRY_AUTHORIZED
  - exact authorization + resume_epoch
  - same worktree resumes
  - WIP preserved
  - worker commits and completes clean

- **E2 Negative stale-fingerprint rejection**
  - separate fresh implementation-worker
  - checkpoint B dirty
  - controller creates a recovery case using the live governance-guard case builder while holding only the Governor wake-up in the controller process
  - controller mutates B -> C in the same allowed file, same workspace and same HEAD
  - controller calls the live production `execution-governance.governance_decide(... RETRY_AUTHORIZED ...)` boundary directly
  - exact live fingerprint revalidation must reject B != C
  - no authorization and no worker spawn may occur
  - the controller then terminalizes its held test case with `HUMAN_REQUIRED` through the normal governance API, preserving the dirty worktree as negative evidence

The positive subtest proves the real event-driven + Execution Governor path. The negative subtest proves the deterministic authorization safety boundary without relying on a timing race.

## Why there is no root orchestrator card

The prior canonical attempt created a dependency cycle:

`root waits for worker` -> `worker waits for root stimulus`.

The new controller runs outside that dependency graph. No LLM root, child dependency, polling agent, or orchestrator materialization is involved.

## Safety

The controller never runs `git reset`, `git clean`, push, merge, cherry-pick, or WIP deletion.
It creates two new worker cards and does not reuse prior Gate E cards/worktrees.

The negative worktree intentionally remains dirty as preserved evidence. Do not integrate either smoke branch.

## Files

- `RUN_GATE_E_CANONICAL.py` — synchronous deterministic controller
- `INSPECT_GATE_E_CANONICAL.py` — read-only inspection of latest/specified controller run
- `VERIFY_GATE_E_CONTROLLER.py` — static/live preflight
- `TEST_PLAN.md` — pass/fail contract

## Recommended container user

Run the controller as Hermes uid/gid `10000:10000` to match the worktree/governance ownership used by workers.

## Output

Each run persists:

`/opt/data/logs/gate-e-canonical/<run_id>/state.json`

and on PASS:

`/opt/data/logs/gate-e-canonical/<run_id>/report.json`

A successful run prints exactly:

`GATE_E_CANONICAL: PASS`

## v1.0.1 preflight fix

The controller no longer runs a full `git status --untracked-files=all` on the repository root.
That scan is not part of Gate E semantics and may exceed 30 seconds on large repositories.
The controller now validates the repository identity + immutable `HEAD`, verifies that the two canonical smoke paths are absent from the base commit, and delegates clean-worktree validation to `worktree-guardian` on each fresh linked worker worktree. Git snapshot timeout is 60 seconds.


## v1.1.0 canonical negative probe

E2 now validates the v2.1 production contract exactly: checkpoint identity is SHA-256 of `git status --porcelain=v1 --untracked-files=all`. Checkpoint B contains one dirty path; controlled checkpoint C adds a second allowed untracked probe path before the production `RETRY_AUTHORIZED` boundary is invoked. This guarantees `status_sha256_B != status_sha256_C` without reset/clean/index mutation and proves stale-status fingerprint rejection. Content-only mutation of an already-dirty path is intentionally out of scope for v2.1.
