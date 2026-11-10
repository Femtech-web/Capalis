from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugin"


class RecordingCtx:
    """Records registrations the way Hermes's PluginContext receives them (v0.23.0 signatures)."""

    def __init__(self) -> None:
        self.tools: dict[str, dict[str, Any]] = {}
        self.commands: dict[str, Any] = {}
        self.hooks: dict[str, Any] = {}

    def register_tool(self, name: str, toolset: str, schema: dict[str, Any], handler: Any,
                      check_fn: Any = None, requires_env: list[str] | None = None, is_async: bool = False,
                      description: str = "", emoji: str = "", override: bool = False) -> None:
        self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "override": override}

    def register_command(self, name: str, handler: Any, description: str = "", args_hint: str = "",
                         argument_mode: str | None = None) -> None:
        self.commands[name] = handler

    def register_hook(self, hook_name: str, callback: Any) -> None:
        self.hooks[hook_name] = callback

    def get_config(self) -> dict[str, Any]:
        return {}


def load_plugin() -> ModuleType:
    spec = importlib.util.spec_from_file_location("capalis_plugin_under_test", PLUGIN / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN)])
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def manifest_list(key: str) -> list[str]:
    lines = (PLUGIN / "plugin.yaml").read_text().splitlines()
    start = lines.index(f"{key}:") if f"{key}:" in lines else None
    if start is None:
        inline = next(line for line in lines if line.startswith(f"{key}:"))
        return [] if inline.strip().endswith("[]") else [inline]
    items = []
    for line in lines[start + 1:]:
        if not line.startswith("  - "):
            break
        items.append(line[4:].strip())
    return items


def test_register_matches_manifest_and_never_overrides() -> None:
    ctx = RecordingCtx()
    load_plugin().register(ctx)
    assert sorted(ctx.tools) == sorted(manifest_list("provides_tools"))
    assert sorted(ctx.hooks) == sorted(manifest_list("provides_hooks")) == ["pre_gateway_dispatch"]
    assert not any(tool["override"] for tool in ctx.tools.values())
    assert set(ctx.commands) == {"capalis"}


def test_model_tools_cannot_approve_resume_or_confirm() -> None:
    ctx = RecordingCtx()
    load_plugin().register(ctx)
    text = json.dumps([tool["schema"] for tool in ctx.tools.values()])
    for word in ("\"approve\"", "\"resume\"", "\"confirm\"", "\"revoke\"", "\"resolve\""):
        assert word not in text, word


def test_registration_opens_no_storage_and_reads_no_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    monkeypatch.setattr(os, "environ", {})
    ctx = RecordingCtx()
    load_plugin().register(ctx)
    assert ctx.hooks["pre_gateway_dispatch"](event=None) is None


CORE = PLUGIN / "capalis" / "core"


@pytest.mark.parametrize("module", sorted(CORE.glob("*.py")), ids=lambda p: p.name)
def test_core_has_no_hermes_or_io_imports(module: Path) -> None:
    forbidden_roots = {"hermes_cli", "agent", "tools", "gateway", "plugins", "model_tools", "socket", "urllib",
                       "http", "requests", "sqlite3", "subprocess", "os"}
    tree = ast.parse(module.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots = {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                assert node.module is None or not node.module.startswith(("adapters", "executor", "storage")), \
                    f"{module.name} imports {node.module}"
                continue
            roots = {(node.module or "").split(".")[0]}
        else:
            continue
        assert not roots & forbidden_roots, f"{module.name} imports {roots & forbidden_roots}"


def test_private_notes_are_never_tracked() -> None:
    ignored = subprocess.run(["git", "check-ignore", "-q", "private-notes/prd.md"], cwd=ROOT, check=False)
    assert ignored.returncode == 0, "private-notes/ must be gitignored"
    tracked = subprocess.run(["git", "ls-files", "private-notes"], cwd=ROOT, capture_output=True, text=True,
                             check=True)
    assert tracked.stdout == ""
