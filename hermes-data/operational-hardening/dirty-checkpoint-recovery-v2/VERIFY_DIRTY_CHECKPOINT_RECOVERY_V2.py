#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
import yaml

MARKER = "HERMES_DIRTY_CHECKPOINT_RECOVERY_V2_2026_09_04"


def must(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/opt/data")
    args = ap.parse_args()
    root = Path(args.root)

    gg_path = root / "plugins/governance-guard/__init__.py"
    gg = gg_path.read_text(encoding="utf-8")
    manifest = yaml.safe_load((root / "plugins/governance-guard/plugin.yaml").read_text(encoding="utf-8")) or {}
    policy = yaml.safe_load((root / "profiles/execution-governor/governance/policy.yaml").read_text(encoding="utf-8")) or {}
    soul = (root / "profiles/implementation-orchestrator/SOUL.md").read_text(encoding="utf-8")

    checks: list[str] = []
    def check(label: str, cond: bool) -> None:
        must(cond, label)
        checks.append(label)

    check("v2 marker", MARKER in gg)
    check("event-driven block hook", 'source="retry_checkpoint_guard_block_hook"' in gg and 'reason_text.startswith("RETRY_CHECKPOINT_GUARD:")' in gg)
    check("request remains non-authorizing", '"retry_authorized": False' in gg and '"target_reactivated": False' in gg)
    check("bounded restart catch-up", "_dirty_recovery_catchup_scan" in gg and "catchup_max_tasks" in gg)
    check("durable guard comment catch-up", "WORKTREE_RETRY_CHECKPOINT_GUARD" in gg and "retry-checkpoint-guard" in gg)
    check("idempotent checkpoint case id", 'case_id = f"dirty-recovery-{tid}-{status_sha256[:16]}"' in gg)
    check("ERROR singleton reconciliation", "_reconcile_error_governor" in gg and "execution_governor_stale_reconciled" in gg)
    check("active/recent case fail-closed", "recent_active_case_requires_human" in gg and "governor_may_still_be_active" in gg)
    check("stale cases preserved, not deleted", 'case["status"] = "STALE"' in gg and '"preserved": True' in gg)
    check("governor missing rebuild", 'board_entry["governor_task_id"] = None' in gg)
    check("governance-guard version", str(manifest.get("version")) == "0.3.2")

    dr = policy.get("dirty_checkpoint_recovery") or {}
    rec = policy.get("execution_governor_reconciliation") or {}
    check("event-driven policy enabled", dr.get("event_driven") is True)
    check("catch-up policy enabled", dr.get("catchup_scan_enabled") is True)
    check("24h stale TTL", int(rec.get("stale_pending_case_ttl_hours") or 0) == 24)
    check("reconciliation policy enabled", rec.get("enabled") is True)
    check("orchestrator policy updated", MARKER in soul and "não depende" in soul)

    compile(gg, str(gg_path), "exec")
    checks.append("governance-guard Python syntax")

    print("VERIFY: PASS")
    for label in checks:
        print(f"  {label}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
