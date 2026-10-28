"""Invoca a Claude Code como el cerebro: sin herramientas y en una carpeta vacía fuera de los repos."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

from coclody.config import ClaudeConfig
from coclody.orchestrator.claude_runner import ClaudeResult, build_invocation, run_claude
from coclody.orchestrator.pool import worker_env


def run_brain(claude: ClaudeConfig, prompt: str, brain_dir: Path, hooks_dir: Path, empty_config: Path) -> ClaudeResult:
    """Corre el cerebro: ``--tools ""`` (sin herramientas, verificado con Claude real), cwd en una
    carpeta vacía propia (no lee un CLAUDE.md del proyecto), con el mismo entorno endurecido que un worker.
    """
    brain_dir.mkdir(parents=True, exist_ok=True)
    cfg = dataclasses.replace(claude, workspace_root=brain_dir, allowed_tools=())
    env = worker_env(os.environ, hooks_dir, empty_config)
    inv = build_invocation(cfg, prompt, workdir_rel=None, base_env=env)
    return run_claude(inv)
