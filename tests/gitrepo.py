"""Repos de git de prueba, armados con git real (sin pasar por gitops, para no probarlo consigo mismo)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from coclody.orchestrator.gitops import Git, git_env

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git no está instalado")


def sh_git(cwd: Path, *args: str) -> str:
    """git directo, con identidad fija y sin hooks globales del que corre los tests."""
    proc = subprocess.run(
        [GIT, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "-c", "commit.gpgsign=false",
         "-c", "init.defaultBranch=main", "-C", str(cwd), *args],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env(), check=False,
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode(errors="replace")


def make_repo(path: Path, branch: str = "main") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    sh_git(path, "init", "-q", "-b", branch)
    # Identidad local del repo: los commits de CoClody la usan (en CI no hay identidad global).
    sh_git(path, "config", "user.name", "Test")
    sh_git(path, "config", "user.email", "test@example.invalid")
    (path / "README.md").write_text("hola\n", encoding="utf-8")
    sh_git(path, "add", "README.md")
    sh_git(path, "commit", "-q", "-m", "inicio")
    return path.resolve()


def make_git(hooks_dir: Path) -> Git:
    return Git(Path(GIT).resolve(), hooks_dir)
