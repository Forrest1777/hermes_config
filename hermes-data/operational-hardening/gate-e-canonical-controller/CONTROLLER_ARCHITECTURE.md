# Controller Architecture

```text
Host operator
  -> RUN_GATE_E_CANONICAL.py (deterministic, synchronous)

E1 positive worker
  -> implementation-worker
  -> retry-checkpoint-guard
  -> governance-guard event-driven recovery
  -> real Execution Governor
  -> execution-governance exact authorization
  -> retry-checkpoint-guard + worktree-guardian accept
  -> same worker/worktree resumes and completes

E2 negative worker
  -> implementation-worker creates B and blocks
  -> controller invokes live governance-guard case builder
     with Governor wake held only in controller process
  -> controller mutates B to C
  -> controller invokes live execution-governance decision boundary
  -> stale B fingerprint must be rejected
  -> no authorization / no spawn
```

No LLM orchestrator root participates in the control plane.


## v1.1.0 canonical negative probe

E2 now validates the v2.1 production contract exactly: checkpoint identity is SHA-256 of `git status --porcelain=v1 --untracked-files=all`. Checkpoint B contains one dirty path; controlled checkpoint C adds a second allowed untracked probe path before the production `RETRY_AUTHORIZED` boundary is invoked. This guarantees `status_sha256_B != status_sha256_C` without reset/clean/index mutation and proves stale-status fingerprint rejection. Content-only mutation of an already-dirty path is intentionally out of scope for v2.1.
