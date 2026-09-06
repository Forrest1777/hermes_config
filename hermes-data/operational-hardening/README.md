# Hermes Operational Hardening Patch — 2026-09-03

This package was prepared from the current logical backup layout inspected in `Forrest1777/hermes_agent_profiles` on 2026-09-03. It does **not** modify GitHub or perform Git commits/pushes.

## What it changes

See `PATCH_MATRIX.md`.

Primary fixes:
- architect human approval lifecycle;
- provider budget `WAITING_PROVIDER` with configurable retry interval;
- governance `resume_epoch`;
- exact dirty-checkpoint recovery authorization;
- focused-tests during iteration + mandatory final full gate;
- GWRM-only Computer Use for implementation workers.

## Safety model

The patch is fail-closed:
- it checks exact anchors from the inspected versions before rewriting large SOUL/plugin files;
- it stops instead of guessing when a target diverges;
- it creates a timestamped backup containing every changed file;
- it never resets/cleans Git worktrees;
- it does not change the Hermes core;
- it does not restart Hermes automatically.

## Recommended: dry-run on live Hermes first

Copy these three files into:

`E:\dev\ai_agents\hermes\hermes-data\operational-hardening\`

- `APPLY_OPERATIONAL_FIXES.py`
- `VERIFY_AFTER_APPLY.py`
- this README (optional)

From `E:\dev\ai_agents\hermes\compose` run:

```powershell
docker compose exec -T hermes /opt/hermes/.venv/bin/python `
  /opt/data/operational-hardening/APPLY_OPERATIONAL_FIXES.py `
  --mode live --root /opt/data --dry-run
```

The command must list the files that would change and end with `DRY RUN: no files written.`

## Apply to live Hermes

Stop/avoid active implementation runs first. Then run:

```powershell
docker compose exec -T hermes /opt/hermes/.venv/bin/python `
  /opt/data/operational-hardening/APPLY_OPERATIONAL_FIXES.py `
  --mode live --root /opt/data
```

A backup directory is created under `/opt/data`, named similar to:

`.operational-hardening-backup-20260903_HHMMSS`

No remote Git operation is performed.

## Verify before restart

```powershell
docker compose exec -T hermes /opt/hermes/.venv/bin/python `
  /opt/data/operational-hardening/VERIFY_AFTER_APPLY.py `
  --mode live --root /opt/data
```

Expected: `VERIFY: PASS`.

Then restart Hermes using your normal Docker Compose procedure so plugins/configs are reloaded. Do not delete the backup until the smoke tests pass.

## Applying to the logical backup repository instead

If Python is available in the checkout:

```powershell
python .\APPLY_OPERATIONAL_FIXES.py --mode backup --root C:\path\to\hermes_agent_profiles --dry-run
python .\APPLY_OPERATIONAL_FIXES.py --mode backup --root C:\path\to\hermes_agent_profiles
python .\VERIFY_AFTER_APPLY.py --mode backup --root C:\path\to\hermes_agent_profiles
```

This only changes the local checkout. Commit/push remain your responsibility.

## Important live-layout note about skills

The backup centralizes `skills/godot-development`. Live Hermes may store/copy that skill under one or more profile directories. In `--mode live`, the patch updates every existing:

`profiles/<implementation-profile>/skills/godot-development/SKILL.md`

If `VERIFY_AFTER_APPLY.py` reports that no live skill path exists, copy the template from:

`templates/skills/godot-development/SKILL.md`

to the location used by your current Hermes profile installation.

## Rollback

Stop Hermes. Copy the files from the timestamped `.operational-hardening-backup-*` directory back to the same relative paths. Then restart Hermes.

Do not restore `state.db*`, Kanban DB files, worktrees, provider runtime state or GWRM runtime state as part of this rollback.

## Before returning to AI ARENA

Execute `TEST_PLAN.md`, especially Gate D (architect approval lifecycle) and Gate E (dirty checkpoint recovery). Only after those pass should the AI ARENA HUMAN/F6 root be resumed with GWRM Computer Use.
