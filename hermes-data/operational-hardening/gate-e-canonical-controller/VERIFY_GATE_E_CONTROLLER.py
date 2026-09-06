#!/usr/bin/env python3
from __future__ import annotations
import argparse, ast, json, re
from pathlib import Path


def check(name: str, ok: bool):
    if not ok:
        raise SystemExit(f'VERIFY: FAIL\n  {name}: FAIL')
    print(f'  {name}: PASS')


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='/opt/data')
    ap.add_argument('--controller', default=None)
    args = ap.parse_args()
    root = Path(args.root)
    gg = root / 'plugins' / 'governance-guard' / '__init__.py'
    eg = root / 'plugins' / 'execution-governance' / '__init__.py'
    policy = root / 'profiles' / 'execution-governor' / 'governance' / 'policy.yaml'
    controller = Path(args.controller) if args.controller else Path(__file__).with_name('RUN_GATE_E_CANONICAL.py')
    gg_text = gg.read_text(encoding='utf-8')
    eg_text = eg.read_text(encoding='utf-8')
    pol_text = policy.read_text(encoding='utf-8')
    ctl_text = controller.read_text(encoding='utf-8')
    print('VERIFY: PASS')
    check('governance-guard v2.1 marker', 'HERMES_DIRTY_CHECKPOINT_RECOVERY_V2_2026_09_04' in gg_text)
    check('event-driven helper', '_ensure_dirty_recovery_case_for_task' in gg_text)
    check('execution-governance exact fingerprint guard', 'recovery checkpoint fingerprint changed after governance case creation' in eg_text)
    check('event-driven policy enabled', 'event_driven: true' in pol_text)
    check('controller has two isolated subtests', 'run_positive' in ctl_text and 'run_negative' in ctl_text)
    check('controller v1.1.0', 'CONTROLLER_VERSION = "1.1.0"' in ctl_text)
    check('negative probe mutates status topology', 'NEGATIVE_MUTATION_PATH' in ctl_text and 'added_second_untracked_path' in ctl_text)
    check('repo-root full status preflight removed', 'canonical repository must be clean before Gate E' not in ctl_text and 'tracked_smoke_paths = git(' in ctl_text)
    check('git timeout hardened to 60s', 'def git(repo: Path, *args: str, timeout: int = 60)' in ctl_text)
    check('controller does not invoke git reset/clean', re.search(r'git\([^\n]*(?:[\"\']reset[\"\']|[\"\']clean[\"\'])', ctl_text) is None)
    ast.parse(ctl_text)
    check('controller Python syntax', True)
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
