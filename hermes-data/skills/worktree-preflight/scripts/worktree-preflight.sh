#!/usr/bin/env bash
set -u
workspace="${HERMES_KANBAN_WORKSPACE:-}"
expected_branch="${HERMES_KANBAN_BRANCH:-}"
base_ref=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --workspace) workspace="$2"; shift 2 ;;
    --expected-branch) expected_branch="$2"; shift 2 ;;
    --base-ref) base_ref="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done
python3 - "$workspace" "$expected_branch" "$base_ref" <<'PY2'
import json, os, pathlib, subprocess, sys
workspace, expected_branch, base_ref = sys.argv[1:4]
result={"passed":False,"workspace":workspace or None,"errors":[],"missing_tracked_files":[],"skip_worktree_entries":[]}
def run(args, check=False, text=True):
    p=subprocess.run(["git","-C",workspace,*args],capture_output=True,text=text)
    if check and p.returncode: raise RuntimeError((p.stderr or p.stdout).strip())
    return p
try:
    if not workspace: raise RuntimeError("HERMES_KANBAN_WORKSPACE ausente")
    real=str(pathlib.Path(workspace).resolve())
    result["workspace"]=real
    if not real.startswith("/workspace/"): raise RuntimeError("workspace fora de /workspace")
    if not pathlib.Path(real).is_dir(): raise RuntimeError("workspace inexistente")
    workspace=real
    inside=run(["rev-parse","--is-inside-work-tree"],True).stdout.strip()
    top=run(["rev-parse","--show-toplevel"],True).stdout.strip()
    common=run(["rev-parse","--path-format=absolute","--git-common-dir"],True).stdout.strip()
    git_dir=run(["rev-parse","--absolute-git-dir"],True).stdout.strip()
    linked_worktree=(pathlib.Path(git_dir).resolve()!=pathlib.Path(common).resolve())
    branch=run(["branch","--show-current"],True).stdout.strip()
    head=run(["rev-parse","HEAD"],True).stdout.strip()
    status=run(["status","--porcelain=v1"],True).stdout
    result.update({"inside_worktree":inside=="true","linked_worktree":linked_worktree,"git_toplevel":top,"git_dir":git_dir,"git_common_dir":common,"branch_expected":expected_branch or None,"branch_actual":branch,"head":head,"base_ref":base_ref or None,"initial_git_clean":not bool(status.strip()),"project_godot_present":pathlib.Path(workspace,"project.godot").is_file(),"agents_md_present":pathlib.Path(workspace,"AGENTS.md").is_file()})
    sparse_checkout=(run(["config","--bool","core.sparseCheckout"]).stdout.strip().lower()=="true")
    sparse_index=(run(["config","--bool","index.sparse"]).stdout.strip().lower()=="true")
    result["sparse_checkout"]=sparse_checkout; result["sparse_index"]=sparse_index
    ls=run(["ls-files","-v"],True).stdout.splitlines()
    result["skip_worktree_entries"]=[line[2:] for line in ls if line.startswith("S ")][:100]
    raw=run(["ls-files","-z"],True,text=False).stdout
    tracked=[x.decode("utf-8","surrogateescape") for x in raw.split(b"\0") if x]
    missing=[]
    for rel in tracked:
        p=pathlib.Path(workspace,rel)
        if not (p.exists() or p.is_symlink()): missing.append(rel)
    result["tracked_file_count"]=len(tracked); result["missing_tracked_files"]=missing[:200]
    if not branch: result["errors"].append("worktree em detached HEAD ou sem branch")
    if expected_branch and branch!=expected_branch: result["errors"].append("branch divergente")
    if base_ref:
        p=run(["merge-base","--is-ancestor",base_ref,"HEAD"])
        result["head_contains_base_ref"]=(p.returncode==0)
        if p.returncode!=0: result["errors"].append("HEAD nao contem base_ref")
    else: result["head_contains_base_ref"]=None
    checks=[inside=="true",linked_worktree,top==workspace,bool(branch),not status.strip(),not sparse_checkout,not sparse_index,not result["skip_worktree_entries"],not missing,result["project_godot_present"],result["agents_md_present"]]
    result["passed"]=all(checks) and not result["errors"]
except Exception as e:
    result["errors"].append(str(e))
print(json.dumps(result,ensure_ascii=False,indent=2))
sys.exit(0 if result["passed"] else 1)
PY2
