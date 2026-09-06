#!/usr/bin/env python3
from __future__ import annotations
import argparse, json
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--root', default='/opt/data')
    ap.add_argument('--run-id')
    args = ap.parse_args()
    base = Path(args.root) / 'logs' / 'gate-e-canonical'
    if args.run_id:
        run_dir = base / args.run_id
    else:
        dirs = sorted([p for p in base.glob('gatee_*') if p.is_dir()])
        if not dirs:
            print('No Gate E controller runs found.')
            return 2
        run_dir = dirs[-1]
    state_path = run_dir / 'state.json'
    if not state_path.is_file():
        print(f'Missing state: {state_path}')
        return 2
    state = json.loads(state_path.read_text(encoding='utf-8'))
    print('GATE E CANONICAL INSPECTION')
    print(f"run_id: {state.get('run_id')}")
    print(f"result: {state.get('result')}")
    print(f"base_ref: {state.get('base_ref')}")
    pos = state.get('positive') or {}
    neg = state.get('negative') or {}
    print(f"positive.task_id: {pos.get('task_id')}")
    print(f"positive.result: {pos.get('result')}")
    print(f"positive.resume_epoch: {pos.get('resume_epoch')}")
    print(f"positive.final_worktree_clean: {pos.get('final_worktree_clean')}")
    print(f"negative.task_id: {neg.get('task_id')}")
    print(f"negative.result: {neg.get('result')}")
    print(f"negative.case_id: {neg.get('case_id')}")
    print(f"negative.stale_case_rejected: {neg.get('stale_case_rejected')}")
    print(f"negative.authorization_created_from_B: {neg.get('authorization_created_from_B')}")
    print(f"negative.worker_spawned_from_stale_case: {neg.get('worker_spawned_from_stale_case')}")
    if state.get('error'):
        print(f"error: {state.get('error')}")
    print(f"state_path: {state_path}")
    report = run_dir / 'report.json'
    if report.is_file():
        print(f"report_path: {report}")
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
