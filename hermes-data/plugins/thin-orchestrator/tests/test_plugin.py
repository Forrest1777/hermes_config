from __future__ import annotations
import importlib.util
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1] / "__init__.py"

def _load():
    spec = importlib.util.spec_from_file_location("thin_orchestrator_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod

def test_checkpoint_fields():
    mod = _load()
    body = """
OPERATIONAL_CHECKPOINT_CANONICAL v3
phase_id: POLISH_DEBUG
integration_head: abc123
main_integrated: true
push_performed: false
"""
    got = mod._checkpoint_fields(body)
    assert got["version"] == 3
    assert got["phase_id"] == "POLISH_DEBUG"
    assert got["integration_head"] == "abc123"
    assert got["main_integrated"] == "true"
    assert got["push_performed"] == "false"

def test_schema_is_bounded():
    mod = _load()
    props = mod.SCHEMA["parameters"]["properties"]
    assert props["recent_events"]["maximum"] == 30
    assert props["recent_comments"]["maximum"] == 30

def test_source_is_read_only_contract():
    source = PLUGIN.read_text(encoding="utf-8")
    for needle in (
        "complete_task(", "block_task(", "unblock_task(", "UPDATE tasks",
        "DELETE FROM tasks", "INSERT INTO tasks", "git commit", "git push",
    ):
        assert needle not in source
