"""Proyectos del orquestador (modo v2) y sus workers.

Un proyecto es una carpeta raíz (un repo de git) con sus worktrees, reglas y
contexto. Hay uno activo por vez (índice único parcial en la base). No se
puede cambiar de proyecto mientras haya tareas ejecutándose: el cambio mueve
de una vez reglas, contexto y worktrees, y una tarea a mitad de camino
quedaría corriendo en un proyecto que ya no es el activo.

Los proyectos se crean solo desde la CLI (``coclody proyecto agregar``): la
carpeta raíz decide qué puede tocar Claude, así que no se elige desde un chat.
Desde Telegram solo se listan y se cambia el activo.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from coclody.db.connection import transaction
from coclody.db.repo import InvalidTransitionError, NotFoundError, RepoError, ValidationError

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")
WORKER_NAME_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,15}")


class ProjectBusyError(RepoError):
    pass


@dataclass(frozen=True)
class Project:
    id: int
    name: str
    root: str
    base_branch: str
    active: int
    created_at: str

    @property
    def root_path(self) -> Path:
        return Path(self.root)


@dataclass(frozen=True)
class WorkerRow:
    project_id: int
    name: str
    branch: str
    path_rel: str
    state: str
    current_task_id: int | None
    last_head: str | None
    updated_at: str


def validate_name(name: object) -> str:
    if not isinstance(name, str) or not _NAME_RE.fullmatch(name):
        raise ValidationError("el nombre del proyecto tiene que ser de 1 a 32 caracteres: a-z, 0-9, - y _")
    return name


def add_project(conn: sqlite3.Connection, name: str, root: Path, base_branch: str) -> Project:
    """Registra un proyecto. ``root`` ya tiene que venir validada (absoluta, resuelta, existente)."""
    name = validate_name(name)
    if not isinstance(root, Path) or not root.is_absolute():
        raise ValidationError("la carpeta raíz tiene que ser una ruta absoluta")
    if not isinstance(base_branch, str) or not 1 <= len(base_branch) <= 200:
        raise ValidationError("rama base inválida")
    with transaction(conn):
        if conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone():
            raise ValidationError(f"ya existe un proyecto llamado {name!r}")
        if conn.execute("SELECT 1 FROM projects WHERE root = ?", (str(root),)).fetchone():
            raise ValidationError("ya hay un proyecto con esa carpeta raíz")
        cur = conn.execute(
            "INSERT INTO projects (name, root, base_branch) VALUES (?, ?, ?)", (name, str(root), base_branch)
        )
        return _get(conn, cur.lastrowid)


def list_projects(conn: sqlite3.Connection) -> list[Project]:
    return [Project(**dict(r)) for r in conn.execute("SELECT * FROM projects ORDER BY name")]


def get_project(conn: sqlite3.Connection, name: str) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
    return Project(**dict(row)) if row else None


def active_project(conn: sqlite3.Connection) -> Project | None:
    row = conn.execute("SELECT * FROM projects WHERE active = 1").fetchone()
    return Project(**dict(row)) if row else None


def running_tasks(conn: sqlite3.Connection) -> list[int]:
    return [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status = 'running' ORDER BY id")]


def activate_project(conn: sqlite3.Connection, name: str) -> Project:
    """Marca ``name`` como el proyecto activo. Falla si hay tareas ejecutándose (de cualquier proyecto)."""
    with transaction(conn):
        project = get_project(conn, name)
        if project is None:
            raise NotFoundError(f"no existe el proyecto {name!r}")
        busy = running_tasks(conn)
        if busy:
            ids = ", ".join(f"#{i}" for i in busy)
            raise ProjectBusyError(f"hay tareas ejecutándose ({ids}); esperá a que terminen o cancelalas")
        conn.execute("UPDATE projects SET active = 0 WHERE active = 1")
        conn.execute("UPDATE projects SET active = 1 WHERE id = ?", (project.id,))
        return _get(conn, project.id)


def sync_workers(conn: sqlite3.Connection, project_id: int, workers: list[tuple[str, str, str]]) -> None:
    """Deja en la base exactamente estos workers (nombre, rama, ruta relativa) para el proyecto.

    Un worker que sobra se borra de la tabla; su worktree queda en disco (CoClody nunca borra worktrees).
    """
    for name, branch, path_rel in workers:
        if not WORKER_NAME_RE.fullmatch(name):
            raise ValidationError(f"nombre de worker inválido: {name!r}")
        if not branch or not path_rel:
            raise ValidationError("rama y ruta del worker son obligatorias")
    names = [w[0] for w in workers]
    with transaction(conn):
        for name, branch, path_rel in workers:
            conn.execute(
                "INSERT INTO workers (project_id, name, branch, path_rel) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (project_id, name) DO UPDATE SET branch = excluded.branch,"
                " path_rel = excluded.path_rel, updated_at = datetime('now')",
                (project_id, name, branch, path_rel),
            )
        placeholders = ",".join("?" * len(names)) or "NULL"
        conn.execute(
            f"DELETE FROM workers WHERE project_id = ? AND name NOT IN ({placeholders})", (project_id, *names)
        )


def list_workers(conn: sqlite3.Connection, project_id: int) -> list[WorkerRow]:
    rows = conn.execute("SELECT * FROM workers WHERE project_id = ? ORDER BY name", (project_id,))
    return [WorkerRow(**dict(r)) for r in rows]


def set_worker_state(conn: sqlite3.Connection, project_id: int, name: str, state: str,
                     current_task_id: int | None = None) -> None:
    if state not in ("idle", "running", "paused", "error"):
        raise ValidationError(f"estado de worker inválido: {state!r}")
    with transaction(conn):
        cur = conn.execute(
            "UPDATE workers SET state = ?, current_task_id = ?, updated_at = datetime('now')"
            " WHERE project_id = ? AND name = ?",
            (state, current_task_id, project_id, name),
        )
        if cur.rowcount != 1:
            raise InvalidTransitionError(f"no existe el worker {name!r} en el proyecto {project_id}")


def _get(conn: sqlite3.Connection, project_id: int) -> Project:
    row = conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"no existe el proyecto id={project_id}")
    return Project(**dict(row))
