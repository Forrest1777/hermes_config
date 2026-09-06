# Post-patch validation plan

Do not resume the AI ARENA HUMAN/F6 root until the operational smoke checks below pass.

## Gate A — static/config validation

Run `VERIFY_AFTER_APPLY.py` inside the Hermes container after applying to `/opt/data`.
Expected: `VERIFY: PASS`.

Additionally restart Hermes so plugin code/config is reloaded, then confirm normal gateway/dispatcher startup without plugin-load errors.

## Gate B — ordinary clean worker smoke

Create a tiny no-op/read-only implementation-worker card with a normal clean worktree.
Expected:
- worktree_guardian_verify PASS;
- no recovery authorization involved;
- normal lifecycle unchanged;
- no native `computer_*` discovery/use.

## Gate C — GWRM GUI discovery smoke

Use the already-known graphical smoke scene.
Expected worker tools include `mcp__gwrm__gui_*`, native `computer_*` is not an active implementation toolset, and GUI Windows smoke still passes.

## Gate D — architect approval lifecycle smoke (highest priority)

Use a deliberately tiny documentation-only architecture decision that really requires human approval.
Expected sequence:

1. architect => `AWAITING_USER_DECISION`;
2. architect persists complete recommended_bundle;
3. architect blocks itself once with `AWAITING_ARCHITECTURE_APPROVAL`;
4. human approves and puts the SAME architect card in READY;
5. next architect run recognizes the already-approved bundle;
6. no `ORCHESTRATOR_ACTION_REQUIRED` handoff is emitted;
7. no `ORCHESTRATOR_MATERIALIZATION` is required;
8. architect updates authorized docs, commits and returns `READY_FOR_INTEGRATION`;
9. orchestrator integrates and continues automatically.

Any second human prompt for the same approved bundle is FAIL.

## Gate E — dirty checkpoint authorization smoke

Use a disposable worker card that leaves a known uncommitted change before a simulated/transient governed retry.
Expected:
- blind redispatch without authorization => blocked by retry-checkpoint-guard;
- governor-authorized retry => exact fingerprint authorization created;
- dispatcher allows the retry;
- `worktree_guardian_verify` reports `authorized_retry_checkpoint=true` and a `resume_epoch`;
- no reset/clean/delete of WIP;
- changed fingerprint => FAIL CLOSED.

## Gate F — provider wait behavior

Prefer a controlled test/stub if available rather than intentionally exhausting production quota.
Expected policy behavior from a captured `usage_limit_reached`/quota error:
- immediate SDK retries disabled;
- task becomes blocked with `PROVIDER_WAIT` and `user_action_required=false`;
- no sleeping worker/session;
- no Execution Governor LLM case for that provider-budget event;
- after configured interval, exactly one task for that provider is promoted as the probe;
- `resume_epoch` increments;
- if quota is still exhausted, it returns to WAITING and schedules the next probe;
- after `max_automatic_retries`, state becomes HUMAN_REQUIRED.

## Gate G — test budget policy

During a small code correction, verify the worker executes focused impacted tests only.
At final consolidated validation, verify one full suite is still mandatory.
