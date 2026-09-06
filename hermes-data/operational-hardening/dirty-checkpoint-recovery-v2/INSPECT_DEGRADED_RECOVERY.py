#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, subprocess
from pathlib import Path


def git(cmd):
    p=subprocess.run(cmd,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=10)
    return p.returncode,(p.stdout or '').strip(),(p.stderr or '').strip()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--root',default='/opt/data')
    ap.add_argument('--task',default='t_b10a9a99')
    ap.add_argument('--board',default='default')
    args=ap.parse_args()
    root=Path(args.root)
    state_path=root/'profiles/execution-governor/governance/state.json'
    state=json.loads(state_path.read_text(encoding='utf-8'))
    board=(state.get('governance_boards') or {}).get(args.board) or {}
    task_state=(state.get('tasks') or {}).get(args.task) or {}
    case_id=task_state.get('last_dirty_recovery_request_case_id')
    case_meta=(state.get('cases') or {}).get(case_id) if case_id else None
    print('DEGRADED RECOVERY INSPECTION')
    print('task:',args.task)
    print('governor.lifecycle:',board.get('lifecycle'))
    print('governor.task_id:',board.get('governor_task_id'))
    print('governor.active_case_id:',board.get('active_case_id'))
    print('governor.pending_cases:',len(board.get('pending_cases') or []))
    print('last_dirty_recovery_case:',case_id)
    print('last_dirty_recovery_case_status:',(case_meta or {}).get('status') if isinstance(case_meta,dict) else None)
    auth=task_state.get('authorized_recovery_checkpoint')
    if isinstance(auth,dict):
        print('authorization.state:',auth.get('state'))
        print('authorization.resume_epoch:',auth.get('resume_epoch'))
        print('authorization.workspace:',auth.get('workspace'))
        print('authorization.head:',auth.get('head'))
        print('authorization.status_sha256:',auth.get('status_sha256'))
    else:
        print('authorization.state: <none>')
    runs=task_state.get('runs') or {}
    vals=[v for v in runs.values() if isinstance(v,dict)]
    vals.sort(key=lambda v:(str(v.get('started_at') or ''),str(v.get('run_id') or '')))
    if vals:
        ws=str(vals[-1].get('workspace_path') or '')
        print('latest.workspace:',ws)
        if ws:
            rc,out,err=git(['git','-C',ws,'status','--porcelain=v1','--untracked-files=all'])
            print('git_status_rc:',rc)
            print('git_status:')
            print(out if out else '<clean>')
            if err: print('git_status_error:',err)
    return 0
if __name__=='__main__': raise SystemExit(main())
