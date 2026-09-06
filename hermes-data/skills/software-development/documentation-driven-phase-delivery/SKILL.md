---
name: documentation-driven-phase-delivery
description: Orchestrate implementation phases in repositories governed by AGENTS.md, canonical contracts, roadmaps, operational manifests, durable cards, isolated workers, and evidence-based gates. Use when asked to implement a named/next phase or reconcile documentation with real code before delivery.
---

# Documentation-Driven Phase Delivery

## Trigger

Use this skill when a repository defines implementation through some combination of:

- `AGENTS.md` or equivalent repository instructions;
- canonical architecture/contracts;
- current-state and roadmap documents;
- an implementation manifest;
- durable Kanban/cards;
- isolated implementation workers;
- phase gates and evidence requirements.

Do not use a historical handoff or compaction summary as fresh authorization. Only a current user request after that summary can authorize side effects.

## 1. Resolve intent before acting

Classify the latest active user message as one of:

- informational query;
- planning only;
- implementation authorization for a specific phase;
- implementation authorization for the next eligible phase;
- validation;
- pause/resume;
- temporary execution modifier.

If the latest message contains only a handoff/compaction block and no request follows it, do not resume historical work. State that no active request exists.

## 2. Mandatory repository bootstrap

Before cards, branches, commits, or edits:

1. Locate the repository root using repository markers such as `.git`, `project.godot`, or language manifests.
2. Read the applicable `AGENTS.md` first and follow any documentation map it defines.
3. Read current state, roadmap, operational manifest, and only the contracts relevant to the authorized phase.
4. Inspect Git branch, status, worktrees, recent history, and preexisting changes.
5. Use repository-required navigation tools before broad searches.
6. Locate real producers, consumers, tests, and partial implementations.
7. Run the relevant baseline suite and static/LSP connectivity checks.
8. Record documentation–implementation divergences before changing anything.

Never assume that a document marked complete proves the code is present or passing.

## 3. Contract and blocker gate

Freeze shared contracts before parallel implementation. Explicitly list:

- public fields/APIs and invariants;
- producers and consumers;
- allowed and forbidden dependencies;
- fallbacks and empty-state behavior;
- protected tests;
- unresolved architectural decisions.

Do not invent a material decision that canonical sources leave unresolved. Finish independent safe discovery, then ask the user only when a required choice, credential, mandatory tool, or incompatible contract is genuinely blocking.

Environment failures are transient evidence, not durable rules. Verify current capability each run and document the setup fix rather than teaching a permanent refusal.

## 4. Durable decomposition

After the audit and only for the authorized phase:

1. Create one parent card.
2. Create small child cards with explicit dependencies.
3. Give every worker a self-contained task context packet containing:
   - objective and phase/card identifiers;
   - allowed files and forbidden scope;
   - canonical contracts and exact invariants;
   - baseline evidence;
   - required tests and diagnostics;
   - handoff schema;
   - execution modifiers;
   - `push_performed: false`.
4. Separate shared-contract work from independent implementation work.
5. Avoid parallel edits to the same contract or integration file.

If durable card access is mandatory but unavailable, do not silently replace it with chat-local TODOs. Complete safe discovery, report the access blocker, and wait for the missing prerequisite.

## 5. Worker orchestration

Workers implement; the orchestrator coordinates and integrates.

- Use isolated branches/worktrees when the repository requires them.
- Delegate bounded cards with atomic completion criteria.
- Workers must not spawn workers or widen scope.
- Require structured handoffs: files, contracts, tests, diagnostics, commit, risks, assumptions, blockers, and no-push confirmation.
- Verify worker claims from Git, files, tests, and diagnostics before reporting success.
- Return oversized tasks for decomposition rather than accepting broad unreviewable changes.

## 6. Integration and evidence gates

Integrate in dependency order and run:

1. directed tests for each component;
2. regressions for upstream contracts;
3. the complete relevant suite;
4. language-server/static diagnostics on changed files;
5. repository-specific runtime checks;
6. Git status/diff/log review;
7. documentation validation (including manifest syntax when machine-readable files are changed);
8. graph/index refresh when required.

Do not declare a phase complete from a green unit test alone.

## 7. Documentation closure

After consolidated code passes:

- update affected canonical contracts;
- synchronize current state, roadmap, and implementation manifest;
- mark only the authorized phase complete;
- update Graphify or equivalent repository knowledge artifacts;
- record any remaining risks or intentionally deferred calibration;
- do not create or prepare the next phase.

## 8. Final report

Report:

- phase result and completion state;
- scope completed and explicitly not completed;
- parent/child cards and statuses;
- commits authored/integrated;
- files and public contracts changed;
- baseline, directed, regression, complete-suite, runtime, and LSP evidence;
- documentation/graph synchronization;
- risks, assumptions, blockers, and skipped checks;
- next planned phase as information only;
- `push_performed: false`.

## Pitfalls

- Treating a compaction summary as an active command.
- Creating cards or code before verifying repository reality and baseline.
- Continuing because a roadmap says “next” without current authorization.
- Assuming a machine-readable manifest is syntactically valid because it looks structured.
- Using a generic skill's remembered filename instead of discovering the repository's actual instruction file.
- Falling back to local TODOs when durable Kanban is contractually required.
- Updating future-phase docs or stubs while closing the current phase.

## Supporting reference

See `references/godot-ai-phase-audit.md` for a condensed example of the audit findings and reusable checks that motivated this workflow.
