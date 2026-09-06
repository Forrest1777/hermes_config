# Hermes Dirty Checkpoint Recovery Request — 2026-09-04

Follow-up hardening for the gap discovered by Gate E (`Dirty Checkpoint Recovery`).

## Problem fixed

The existing hardening correctly rejected a blind redispatch of a dirty worktree, but there was no governed transition from that fail-closed block into an Execution Governor case. After the second block, Hermes' native block-loop breaker could move the child to `triage`, leaving no `authorized_recovery_checkpoint` and no `resume_epoch`.

## New flow

```text
implementation-orchestrator
  -> request_dirty_checkpoint_recovery(target_task_id, reason)
  -> validates related child + no active run + blocked/triage + exact dirty Git checkpoint
  -> creates PENDING DIRTY_CHECKPOINT_RECOVERY case
  -> ensures persistent Execution Governor
  -> DOES NOT authorize or reactivate target

Execution Governor
  -> governance_context includes case_type + validated recovery facts
  -> RETRY_AUTHORIZED | HUMAN_REQUIRED

execution-governance on RETRY_AUTHORIZED
  -> re-reads live workspace
  -> requires exact workspace + HEAD + git-status SHA256 match
  -> creates authorized_recovery_checkpoint + resume_epoch
  -> blocked: native unblock
  -> triage: narrow CAS triage->ready, only for DIRTY_CHECKPOINT_RECOVERY

retry-checkpoint-guard + worktree-guardian
  -> allow only the exact authorized fingerprint
  -> worker resumes same worktree and WIP
```

## Authority boundaries

`request_dirty_checkpoint_recovery` is a request only. It cannot:
- create `authorized_recovery_checkpoint`;
- increment `resume_epoch`;
- move the child to `ready`;
- reset/clean/delete/reprovision the worktree.

Only Execution Governor `RETRY_AUTHORIZED`, followed by deterministic execution-governance validation, can authorize/requeue.

## Files changed

- `/opt/data/plugins/governance-guard/__init__.py`
- `/opt/data/plugins/governance-guard/plugin.yaml` -> 0.3.1
- `/opt/data/plugins/execution-governance/__init__.py`
- `/opt/data/plugins/execution-governance/plugin.yaml` -> 0.3.1
- `/opt/data/profiles/implementation-orchestrator/SOUL.md`
- `/opt/data/profiles/execution-governor/SOUL.md`

No Hermes core/Kanban source is modified.

## Apply

Copy `APPLY_DIRTY_CHECKPOINT_RECOVERY.py` and `VERIFY_DIRTY_CHECKPOINT_RECOVERY.py` to a writable location under `/opt/data`, for example:

`/opt/data/operational-hardening/dirty-checkpoint-recovery/`

Dry-run first:

```powershell
cd E:\dev\ai_agents\hermes\compose

docker compose exec -T hermes /opt/hermes/.venv/bin/python `
  /opt/data/operational-hardening/dirty-checkpoint-recovery/APPLY_DIRTY_CHECKPOINT_RECOVERY.py `
  --root /opt/data --dry-run
```

Expected: 6 files requiring changes and `DRY RUN: no files written.`

Apply:

```powershell
docker compose exec -T hermes /opt/hermes/.venv/bin/python `
  /opt/data/operational-hardening/dirty-checkpoint-recovery/APPLY_DIRTY_CHECKPOINT_RECOVERY.py `
  --root /opt/data
```

Verify:

```powershell
docker compose exec -T hermes /opt/hermes/.venv/bin/python `
  /opt/data/operational-hardening/dirty-checkpoint-recovery/VERIFY_DIRTY_CHECKPOINT_RECOVERY.py `
  --root /opt/data
```

Expected: `VERIFY: PASS`.

Then recreate/restart Hermes so plugin managers reload:

```powershell
docker compose down
docker compose up -d
docker compose ps
```

## Existing Gate E smoke

Do not discard the current dirty checkpoint. See `RECOVER_CURRENT_SMOKE.md` for the intended continuation of the existing root/worker rather than starting a new smoke.
