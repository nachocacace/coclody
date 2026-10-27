"""Operaciones de proyecto que usan la CLI, Telegram y el arranque.

Un proyecto se activa solo si su raíz es válida y todos sus worktrees quedan
bien (validados o recién creados). Si algo falla, el proyecto activo no cambia.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from coclody.config import Config
from coclody.db import DbFactory
from coclody.db import projects as prepo
from coclody.db.repo import RepoError
from coclody.orchestrator.gitops import Git, GitError
from coclody.orchestrator.worktrees import WorktreeError, WorktreeReport, ensure_worktrees, validate_root, worker_specs


class ProjectError(Exception):
    def __init__(self, message: str, reports: list[WorktreeReport] | None = None):
        super().__init__(message)
        self.reports = reports or []


@dataclass(frozen=True)
class Activation:
    project: prepo.Project
    reports: list[WorktreeReport]


def add(db: DbFactory, git: Git, config: Config, name: str, root: Path) -> prepo.Project:
    """Registra un proyecto nuevo (sin activarlo). Solo desde la CLI."""
    try:
        prepo.validate_name(name)
        resolved, base = validate_root(git, root, config.claude.workspace_root)
        with db() as conn:
            return prepo.add_project(conn, name, resolved, base)
    except (WorktreeError, RepoError) as exc:
        raise ProjectError(str(exc)) from None


def activate(db: DbFactory, git: Git, config: Config, name: str) -> Activation:
    """Valida la raíz, crea los worktrees que falten y marca el proyecto como activo."""
    with db() as conn:
        project = prepo.get_project(conn, name)
        busy = prepo.running_tasks(conn)
    if project is None:
        raise ProjectError(f"no existe el proyecto {name!r}")
    if busy:
        raise ProjectError(f"hay tareas ejecutándose ({', '.join(f'#{i}' for i in busy)}); esperá a que terminen")
    reports = prepare(db, git, config, project)
    try:
        with db() as conn:
            return Activation(prepo.activate_project(conn, name), reports)
    except RepoError as exc:
        raise ProjectError(str(exc), reports) from None


def prepare(db: DbFactory, git: Git, config: Config, project: prepo.Project) -> list[WorktreeReport]:
    """Valida la raíz, crea los worktrees que falten y registra los workers. Lanza si algo queda mal."""
    reports = inspect(git, config, project, create=True)
    bad = [r for r in reports if r.status not in ("ok", "created")]
    if bad:
        raise ProjectError("hay worktrees con problemas", reports)
    with db() as conn:
        prepo.sync_workers(conn, project.id, [(r.spec.name, r.spec.branch, r.spec.path_rel) for r in reports])
    return reports


def inspect(git: Git, config: Config, project: prepo.Project, *, create: bool = False) -> list[WorktreeReport]:
    """Valida la raíz y los worktrees. Sin ``create`` no toca nada (lo usa ``coclody check``)."""
    try:
        root, base = validate_root(git, project.root_path, config.claude.workspace_root)
        if base != project.base_branch:
            raise WorktreeError(
                f"la carpeta raíz está en {base!r} y el proyecto se registró con {project.base_branch!r}; "
                "volvé a esa rama"
            )
        return ensure_worktrees(git, root, base, worker_specs(config.orchestrator.workers), create=create)
    except (WorktreeError, GitError) as exc:
        raise ProjectError(str(exc)) from None
