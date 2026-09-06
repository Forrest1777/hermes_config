#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
from pathlib import Path

import yaml

MARKER = "HERMES_DIRTY_CHECKPOINT_RECOVERY_2026_09_04"


def must(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="/opt/data")
    args = parser.parse_args()
    root = Path(args.root)

    gg = (root / "plugins/governance-guard/__init__.py").read_text(encoding="utf-8")
    eg = (root / "plugins/execution-governance/__init__.py").read_text(encoding="utf-8")
    osoul = (root / "profiles/implementation-orchestrator/SOUL.md").read_text(encoding="utf-8")
    gsoul = (root / "profiles/execution-governor/SOUL.md").read_text(encoding="utf-8")
    ggm = yaml.safe_load((root / "plugins/governance-guard/plugin.yaml").read_text(encoding="utf-8"))
    egm = yaml.safe_load((root / "plugins/execution-governance/plugin.yaml").read_text(encoding="utf-8"))

    checks = []
    def check(label: str, condition: bool) -> None:
        must(condition, label)
        checks.append(label)

    check("REQUEST_DIRTY_CHECKPOINT_RECOVERY tool", MARKER in gg and 'name="request_dirty_checkpoint_recovery"' in gg and 'toolset=DIRTY_RECOVERY_TOOLSET' in gg)
    check("Request does not authorize/reactivate directly", '"retry_authorized": False' in gg and '"target_reactivated": False' in gg)
    check("Related-card + blocked/triage validation", '_dirty_recovery_related' in gg and 'target_status not in {"blocked", "triage"}' in gg)
    check("Governance case creation", '"case_type": "DIRTY_CHECKPOINT_RECOVERY"' in gg and '_ensure_board_governor(None, case, board, policy)' in gg)
    check("Execution Governor context facts", '"case_type": case.get("case_type")' in eg and '"recovery_request_validated"' in eg)
    check("Exact live fingerprint revalidation", '_live_retry_checkpoint' in eg and 'fingerprint changed after governance case creation' in eg)
    check("Resume epoch authorization preserved", 'retry_checkpoint_authorized' in eg and 'resume_epoch' in eg)
    check("Governed triage recovery", 'governance_recovery_requeued' in eg and 'task_status == "triage" and recovery_case' in eg)
    check("Orchestrator routing policy", MARKER in osoul and 'request_dirty_checkpoint_recovery' in osoul)
    check("Governor decision policy", MARKER in gsoul and 'DIRTY_CHECKPOINT_RECOVERY' in gsoul)
    check("governance-guard version", str((ggm or {}).get("version")) == "0.3.1")
    check("execution-governance version", str((egm or {}).get("version")) == "0.3.1")

    compile(gg, str(root / "plugins/governance-guard/__init__.py"), "exec")
    checks.append("governance-guard Python syntax")
    compile(eg, str(root / "plugins/execution-governance/__init__.py"), "exec")
    checks.append("execution-governance Python syntax")

    print("VERIFY: PASS")
    for label in checks:
        print(f"  {label}: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
