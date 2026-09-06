# Patch matrix

| File | Change |
|---|---|
| `plugins/governance-guard/__init__.py` | event-driven recovery case creation, restart catch-up, stale ERROR singleton reconciliation |
| `plugins/governance-guard/plugin.yaml` | version 0.3.1 → 0.3.2 |
| `profiles/execution-governor/governance/policy.yaml` | event-driven/catch-up/reconciliation settings, TTL 24h |
| `profiles/implementation-orchestrator/SOUL.md` | documents event-driven canonical flow; manual request is fallback |

No change to execution-governance v0.3.1: its exact fingerprint authorization, `resume_epoch`, and governed `triage → ready` path remain the authority boundary.
