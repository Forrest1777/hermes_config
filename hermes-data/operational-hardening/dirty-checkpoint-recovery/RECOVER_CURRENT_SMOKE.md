# Continue the existing Gate E smoke

Current preserved cards from the failed Gate E attempt:

- root/orchestrator: `t_ef479657`
- worker: `t_b10a9a99`
- worktree: `/workspace/skill_system_framework/.worktrees/t_b10a9a99`
- expected preserved HEAD: `59f652c5d49b1b4b7a52fb83782bf9d786d616e4`
- expected preserved status hash: `cdd81f05c403873b2ac4644f100ee74cfe1824ce967e26e0cf9dea336cb00d74`
- preserved path: `docs/_smoke/dirty-checkpoint-recovery-smoke.md`

Do not reset, clean, delete, reprovision, or create a replacement worktree.

After patch + VERIFY + Hermes restart:

1. Add this recovery instruction to root `t_ef479657`:

```text
OPERATIONAL_HARDENING_RESUME — Gate E dirty checkpoint recovery

The REQUEST_DIRTY_CHECKPOINT_RECOVERY gap has been patched and verified.
Continue the EXISTING smoke; do not create a replacement worker/worktree.

Target worker: t_b10a9a99
Expected preserved worktree: /workspace/skill_system_framework/.worktrees/t_b10a9a99

Required next action:
- call request_dirty_checkpoint_recovery exactly once for t_b10a9a99;
- reason: "Gate E continuation after blind dirty redispatch fail-closed; preserve exact existing WIP";
- do NOT call orchestration_unblock_card for this recovery;
- do NOT manually create authorized_recovery_checkpoint/state.json;
- after the request is accepted, wait by dependency/handoff; do not poll;
- Execution Governor must own RETRY_AUTHORIZED/HUMAN_REQUIRED;
- if authorized, continue the same worker/worktree with the existing smoke Phase B;
- preserve all existing WIP and evidence;
- never reset/clean/delete/reprovision;
- never push or integrate the smoke commit.
```

2. Put the SAME root `t_ef479657` in READY.
3. Do not manually move `t_b10a9a99` from triage. The new governed path must do that only after RETRY_AUTHORIZED.

Expected new evidence:
- `dirty_checkpoint_recovery_requested`
- `case_type=DIRTY_CHECKPOINT_RECOVERY`
- Governor `RETRY_AUTHORIZED`
- `retry_checkpoint_authorized`
- `resume_epoch > 0`
- `governance_recovery_requeued` from triage
- `authorized_retry_checkpoint_allowed`
- worktree_guardian `authorized_retry_checkpoint=true`
