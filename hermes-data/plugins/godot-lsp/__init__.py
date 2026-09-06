"""Plugin Hermes para GDScript LSP dinamico por worktree via GWRM."""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("hermes_plugins.godot_lsp")
PLUGIN_DIR = Path(__file__).resolve().parent
SERVER_ID = "godot-gdscript"


def _find_godot_root(file_path: str, workspace: str) -> str | None:
    file_parent = Path(file_path).resolve().parent
    workspace_path = Path(workspace).resolve()
    current = file_parent
    while True:
        if (current / "project.godot").is_file():
            return str(current)
        if current == workspace_path or current.parent == current:
            break
        try:
            current.relative_to(workspace_path)
        except ValueError:
            break
        current = current.parent
    if (workspace_path / "project.godot").is_file():
        return str(workspace_path)
    return None


def _bridge_command(ctx: Any) -> list[str] | None:
    configured = ctx.binary_overrides.get(SERVER_ID)
    if isinstance(configured, list) and configured:
        return [str(part) for part in configured]
    bridge = shutil.which("godot-lsp-bridge")
    if bridge:
        return [bridge]
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    candidate = hermes_home / "bin" / "godot-lsp-bridge"
    return [str(candidate)] if candidate.is_file() else None


def _build_spawn(root: str, ctx: Any) -> Any:
    from agent.lsp.servers import SpawnSpec
    command = _bridge_command(ctx)
    if command is None:
        LOGGER.warning("godot-lsp-bridge nao encontrado.")
        return None
    env = dict(ctx.env_overrides.get(SERVER_ID, {}))
    env["GWRM_PROJECT_ROOT"] = root
    return SpawnSpec(
        command=command,
        workspace_root=root,
        cwd=root,
        env=env,
        initialization_options=dict(ctx.init_overrides.get(SERVER_ID, {})),
        seed_diagnostics_on_first_push=True,
    )


def _server_env() -> dict[str, str]:
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        lsp_cfg = cfg.get("lsp") if isinstance(cfg, dict) else {}
        servers = lsp_cfg.get("servers") if isinstance(lsp_cfg, dict) else {}
        server = servers.get(SERVER_ID) if isinstance(servers, dict) else {}
        raw = server.get("env") if isinstance(server, dict) else {}
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _setting(env: dict[str, str], name: str, default: str = "") -> str:
    return env.get(name) or os.environ.get(name) or default


def _derive_worktree(project_path: str, container_root: str) -> str | None:
    project = project_path.replace("\\", "/").rstrip("/")
    root = container_root.replace("\\", "/").rstrip("/")
    if project == root:
        return None
    if project.startswith(root + "/"):
        remainder = project[len(root) + 1:]
        return remainder.split("/", 1)[0] or None
    return None


def _gwrm_status(project_path: str | None = None) -> dict[str, Any]:
    env = _server_env()
    project = str(Path(project_path or os.getcwd()).resolve())
    worktrees_root = _setting(env, "GWRM_CONTAINER_WORKTREES_ROOT", "/workspace/skill_system_framework/.worktrees")
    worktree = _setting(env, "GWRM_WORKTREE_NAME") or _derive_worktree(project, worktrees_root)
    control_url = _setting(env, "GWRM_CONTROL_URL", "http://host.docker.internal:8130").rstrip("/")
    api_key = _setting(env, "GWRM_API_KEY")
    payload: dict[str, Any] = {
        "project_root": project,
        "worktree_name": worktree,
        "control_url": control_url,
        "configured": bool(worktree and api_key),
    }
    if not worktree or not api_key:
        payload["error"] = "Nao foi possivel resolver worktree_name ou GWRM_API_KEY."
        return payload
    try:
        req = Request(
            f"{control_url}/api/v1/worktrees/{quote(worktree, safe='')}",
            headers={"X-API-Key": api_key},
            method="GET",
        )
        with urlopen(req, timeout=3) as response:
            payload["session"] = json.loads(response.read().decode("utf-8"))
            payload["reachable"] = True
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        payload["reachable"] = False
        payload["error"] = str(exc)
    return payload


def _tool_handler(params: dict[str, Any], **_: Any) -> str:
    return json.dumps(_gwrm_status(params.get("project_path")), ensure_ascii=False, indent=2)


def register(ctx: Any) -> None:
    from agent.lsp.servers import LANGUAGE_BY_EXT, SERVERS, ServerDef
    LANGUAGE_BY_EXT[".gd"] = "gdscript"
    existing = next((s for s in SERVERS if s.server_id == SERVER_ID or ".gd" in s.extensions), None)
    if existing is None:
        SERVERS.append(ServerDef(
            server_id=SERVER_ID,
            extensions=(".gd",),
            resolve_root=_find_godot_root,
            build_spawn=_build_spawn,
            seed_first_push=True,
            description="GDScript - Godot LSP isolado por worktree via GWRM",
        ))
    ctx.register_tool(
        name="godot_lsp_status",
        toolset="godot_lsp",
        schema={
            "name": "godot_lsp_status",
            "description": "Consulta o GWRM e mostra a sessao LSP da worktree atual.",
            "parameters": {
                "type": "object",
                "properties": {"project_path": {"type": "string"}},
            },
        },
        handler=_tool_handler,
    )
    ctx.register_command(
        name="godot-lsp-status",
        handler=lambda raw: json.dumps(_gwrm_status(raw.strip() or None), ensure_ascii=False, indent=2),
        description="Mostra a sessao LSP dinamica da worktree via GWRM.",
        args_hint="[project_path]",
    )
    ctx.register_skill(
        name="godot-development",
        path=PLUGIN_DIR / "skills" / "godot-development" / "SKILL.md",
        description="Praticas de GDScript, GWRM, LSP e GUT.",
    )
