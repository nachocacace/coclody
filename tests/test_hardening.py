"""Endurecimiento del orquestador (hallazgos de la revisión externa).

Cubre lo que no necesita Claude real (eso se verificó aparte, en /tmp): denegación de .git en la
invocación, settings del sandbox, nonce del marcador de preguntas, archivos sensibles, límite de uso,
tope de reintentos, keepawake, identidad de git y el comando /worker.
"""

from __future__ import annotations

import os
import sys

import pytest

from coclody.config import ConfigError, load_config
from coclody.orchestrator import sensitive
from coclody.orchestrator.claude_runner import _is_usage_limit
from coclody.orchestrator.pool import DENY_TOOLS, sandbox_available, sandbox_settings, worker_env, worker_tools
from coclody.orchestrator.worker import QUESTION_BASE, defang_markers, new_nonce, parse_question, question_markers


def toml_path(path) -> str:
    return str(path).replace("\\", "\\\\")


# --- hallazgo 2: .git por construcción (reglas de denegación en la invocación) ---------------------------


def test_deny_tools_cover_git_pointer_and_config_files():
    paths = {d[d.index("(") + 1:-1] for d in DENY_TOOLS}
    assert {".git", ".git/**", "**/.git", "**/.git/**", ".gitattributes", "**/.gitattributes",
            ".gitmodules", "**/.gitmodules"} <= paths
    assert {d.split("(")[0] for d in DENY_TOOLS} == {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def test_build_invocation_passes_deny_and_settings(tmp_path):
    from coclody.config import ClaudeConfig
    from coclody.orchestrator.claude_runner import build_invocation

    ws = tmp_path / "ws"
    ws.mkdir()
    exe = tmp_path / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    exe.parent.mkdir()
    exe.write_bytes(b"#!/bin/sh\n")
    exe.chmod(0o755)
    cfg = ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=("Read(./**)",), timeout_minutes=5)
    inv = build_invocation(cfg, "hola", deny_tools=("Write(.git)", "Edit(**/.git/**)"), settings_json='{"sandbox": {}}')
    argv = list(inv.argv)
    assert argv[argv.index("--disallowedTools") + 1] == "Write(.git),Edit(**/.git/**)"
    assert argv[argv.index("--settings") + 1] == '{"sandbox": {}}'


# --- hallazgo 1/3: sandbox settings ----------------------------------------------------------------------


def test_sandbox_settings_confine_to_the_worktree():
    import json
    from pathlib import Path

    root, wt = Path("/proj"), Path("/proj/.coclody/worktrees/w1")
    s = json.loads(sandbox_settings(root, wt))["sandbox"]
    assert s["enabled"] and s["failIfUnavailable"] and not s["allowUnsandboxedCommands"]
    assert s["network"]["allowedDomains"] == []  # red cortada del todo
    # Rutas con el separador de la plataforma (en Windows, "\").
    assert s["filesystem"]["denyRead"] == [str(root)] and s["filesystem"]["allowRead"] == [str(wt)]


def test_sandbox_available_by_platform():
    assert sandbox_available() == (sys.platform in ("darwin", "linux"))


def test_worker_commands_rejected_without_sandbox(app_paths, tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    app_paths.config_file.write_text(
        f'[telegram]\nallowed_chat_ids=[1]\n[claude]\nworkspace_root="{toml_path(ws)}"\n'
        '[orquestador]\nenabled=true\nworker_commands=["pytest"]\n', encoding="utf-8")
    monkeypatch.setattr("coclody.config._sandbox_platform", lambda: False)
    with pytest.raises(ConfigError, match="sandbox"):
        load_config(app_paths)
    monkeypatch.setattr("coclody.config._sandbox_platform", lambda: True)
    assert load_config(app_paths).orchestrator.worker_commands == ("pytest",)


# --- hallazgo 3: nonce del marcador de preguntas ---------------------------------------------------------


def test_marker_needs_the_runs_nonce():
    nonce = new_nonce()
    sino, abierta = (m for m, _ in question_markers(nonce))
    assert parse_question(f"{abierta} ¿X?", nonce) == ("¿X?", "open")
    assert parse_question(f"{sino} ¿X?", nonce) == ("¿X?", "yes_no")
    # Un archivo citado con el marcador base, u otro nonce, no dispara una pregunta.
    assert parse_question(f"{QUESTION_BASE}: ¿falsa?", nonce) is None
    assert parse_question(f"{QUESTION_BASE}_{new_nonce()}: ¿otra?", nonce) is None
    assert parse_question("texto\nCOCLODY_PREGUNTA: citado de un archivo", nonce) is None


def test_defang_markers_breaks_the_word_but_keeps_it_readable():
    out = defang_markers("Vi COCLODY_PREGUNTA: en config.py")
    assert "COCLODY_PREGUNTA:" not in out and "COCLODY_PREG" in out and "config.py" in out
    assert defang_markers("nada") == "nada"


# --- hallazgo 7: archivos sensibles ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "category"),
    [
        (".github/workflows/ci.yml", "CI"),
        ("sub/.github/workflows/x.yaml", "CI"),
        ("conftest.py", "build/test"),
        ("frontend/package.json", "build/test"),
        ("Makefile", "build/test"),
        ("Dockerfile", "build/test"),
        (".claude/settings.json", "editor/Claude"),
        ("CLAUDE.md", "editor/Claude"),
        (".vscode/tasks.json", "editor/Claude"),
        ("scripts/deploy.sh", "hooks/scripts"),
        ("requirements.txt", "dependencias"),
    ],
)
def test_sensitive_classification(path, category):
    assert category in sensitive.classify([path])


def test_sensitive_ignores_normal_files():
    assert sensitive.classify(["src/app.py", "README.md", "docs/guia.md", "tests/test_x.py"]) == []


def test_sensitive_is_ordered_and_deduped():
    cats = sensitive.classify([".github/workflows/a.yml", "package.json", "b.github/workflows/c.yml"])
    assert cats == ["CI", "build/test"]


# --- hallazgo 5c: detección del límite de uso ------------------------------------------------------------


@pytest.mark.parametrize("text", ["Claude usage limit reached", "429 Too Many Requests", "rate limit exceeded",
                                  "límite de uso alcanzado", "resource_exhausted", "insufficient_quota", "overloaded"])
def test_usage_limit_is_detected(text):
    assert _is_usage_limit(text)


@pytest.mark.parametrize("text", ["", "error de sintaxis en app.py", "no such file", "el test falló"])
def test_non_usage_errors_are_not_flagged(text):
    assert not _is_usage_limit(text)


# --- env sin secretos (hallazgo 1, mitigación) -----------------------------------------------------------


def test_worker_env_drops_secrets_and_ssh(tmp_path):
    base = {"HOME": "/h", "PATH": "/bin", "AWS_SECRET_ACCESS_KEY": "x", "GH_TOKEN": "y", "MY_API_KEY": "z",
            "DB_PASSWORD": "p", "SSH_AUTH_SOCK": "/s", "CLAUDE_CODE_OAUTH_TOKEN": "keep", "GIT_DIR": "/g"}
    env = worker_env(base, tmp_path / "h", tmp_path / "c")
    assert env["HOME"] == "/h" and env["PATH"] == "/bin" and env["CLAUDE_CODE_OAUTH_TOKEN"] == "keep"
    assert not {"AWS_SECRET_ACCESS_KEY", "GH_TOKEN", "MY_API_KEY", "DB_PASSWORD", "SSH_AUTH_SOCK", "GIT_DIR"} & set(env)


def test_worker_tools_shape():
    assert worker_tools(("pytest", "npm test")) == (
        "Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./**)", "Write(./**)", "Bash(pytest:*)", "Bash(npm test:*)")
