#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import py_compile
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("hardening_patch", HERE / "APPLY_OPERATIONAL_FIXES.py")
PATCH = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(PATCH)


def active_platform_has_native_computer(text: str) -> bool:
    start = text.find("platform_toolsets:")
    if start < 0:
        return False
    end = text.find("\nplugins:", start)
    section = text[start:] if end < 0 else text[start:end]
    return "    - computer_use\n" in section


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--mode", choices=("backup", "live"), required=True)
    args = ap.parse_args()
    root = Path(args.root).resolve()
    m = PATCH.paths_for(root, args.mode)
    errors: list[str] = []

    def text(key: str) -> str:
        return Path(m[key]).read_text(encoding="utf-8-sig")

    arch = text("architect_soul")
    orch = text("orchestrator_soul")
    worker = text("worker_soul")
    gov_soul = text("governor_soul")
    if "Protocolo de espera e retomada da aprovação" not in arch:
        errors.append("architect SOUL: new approval resume protocol missing")
    if "Disciplina de contexto e budget" not in orch:
        errors.append("orchestrator SOUL: context/budget discipline missing")
    if "Test impact policy" not in worker or "GWRM GUI policy" not in worker:
        errors.append("worker SOUL: test-impact or GWRM GUI policy missing")
    if "provider-wait scheduler" not in gov_soul:
        errors.append("execution-governor SOUL: provider-wait policy missing")

    for key in ("architect_config", "orchestrator_config", "worker_config"):
        cfg_text = text(key)
        if active_platform_has_native_computer(cfg_text):
            errors.append(f"{key}: native computer_use still active")
    if "        - gui_status\n" not in text("worker_config") or "        - gui_click\n" not in text("worker_config"):
        errors.append("worker config: GWRM gui_* MCP allowlist missing")

    try:
        policy = yaml.safe_load(Path(m["policy"]).read_text(encoding="utf-8"))
        pr = (policy or {}).get("provider_recovery") or {}
        pc = (policy or {}).get("provider_circuit") or {}
        if not pr.get("enabled"):
            errors.append("policy: provider_recovery not enabled")
        if int(pr.get("retry_interval_minutes") or 0) <= 0:
            errors.append("policy: invalid retry_interval_minutes")
        if pc.get("require_human_reset") is not False:
            errors.append("policy: provider circuit still requires human reset")
    except Exception as exc:
        errors.append(f"policy YAML invalid: {exc}")

    plugins = Path(m["plugins"])
    plugin_files = [
        plugins / "governance-guard" / "__init__.py",
        plugins / "execution-governance" / "__init__.py",
        plugins / "retry-checkpoint-guard" / "__init__.py",
        plugins / "worktree-guardian" / "__init__.py",
    ]
    required_markers = {
        "governance-guard": "provider_wait_resumed",
        "execution-governance": "retry_checkpoint_authorized",
        "retry-checkpoint-guard": "authorized_retry_checkpoint_allowed",
        "worktree-guardian": "authorized_retry_checkpoint",
    }
    for path in plugin_files:
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as exc:
            errors.append(f"Python syntax: {path}: {exc}")
        marker = required_markers[path.parent.name]
        if marker not in path.read_text(encoding="utf-8"):
            errors.append(f"{path.parent.name}: marker {marker} missing")

    skill_paths = m["skills"]
    if not skill_paths:
        print("NOTE: no live godot-development skill path found; update/copy the centralized skill manually if your live layout stores it elsewhere.")
    for p in skill_paths:
        if "GUI Windows / Computer Use" not in Path(p).read_text(encoding="utf-8-sig"):
            errors.append(f"skill not updated: {p}")

    if errors:
        print("VERIFY: FAIL")
        for e in errors:
            print(f"  - {e}")
        return 1
    print("VERIFY: PASS")
    print("  Architect approval lifecycle markers: PASS")
    print("  Provider wait policy: PASS")
    print("  Dirty checkpoint authorization: PASS")
    print("  Native implementation computer_use disabled: PASS")
    print("  Worker GWRM GUI allowlist: PASS")
    print("  Python plugin syntax: PASS")
    print("  YAML policy parse: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
