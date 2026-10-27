"""Confinamiento de Claude Code a la carpeta de trabajo.

Qué garantiza: el directorio de trabajo de cada ejecución es una carpeta real
dentro de ``workspace_root`` (después de resolver ``..`` y symlinks), y el
ejecutable de Claude Code no vive dentro de esa carpeta (no se puede plantar
un ``claude.exe`` falso en un proyecto).

Qué NO garantiza: no es un sandbox del sistema operativo. Las herramientas que
se habiliten (sobre todo Bash) corren con los permisos del usuario. El límite
real lo ponen ``allowed_tools`` y el modo de permisos con que se invoca a
Claude Code (ver claude_runner). Entre la verificación y el arranque del
proceso, un symlink podría cambiar; por eso la verificación se hace justo
antes de lanzar.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from coclody.db.repo import ValidationError, normalize_workdir_rel


class SandboxError(Exception):
    pass


def resolve_workdir(workspace_root: Path, workdir_rel: str | None) -> Path:
    """Carpeta de trabajo absoluta y resuelta para una tarea.

    ``workdir_rel`` es relativo a ``workspace_root``; ``None`` es la raíz.
    La carpeta tiene que existir: no se crea nada.
    """
    root = _resolve_dir(workspace_root, "la carpeta de trabajo raíz")
    try:
        rel = normalize_workdir_rel(workdir_rel)
    except ValidationError as exc:
        raise SandboxError(str(exc)) from None
    if rel is None:
        return root
    target = _resolve_dir(root.joinpath(*rel.split("/")), f"la subcarpeta {rel!r}")
    if not target.is_relative_to(root):
        raise SandboxError(f"la subcarpeta {rel!r} apunta fuera de la carpeta de trabajo")
    return target


def resolve_executable(name_or_path: str, workspace_root: Path) -> Path:
    """Ruta absoluta del ejecutable de Claude Code, verificada.

    Un nombre sin separadores se busca en el PATH; una ruta tiene que ser
    absoluta. En ambos casos el resultado no puede estar dentro de la carpeta
    de trabajo.
    """
    if "/" in name_or_path or "\\" in name_or_path:
        candidate = Path(name_or_path)
        if not candidate.is_absolute():
            raise SandboxError("la ruta del ejecutable tiene que ser absoluta")
        if not candidate.is_file() or not os.access(candidate, os.X_OK):
            raise SandboxError(f"no existe o no es ejecutable: {candidate}")
    else:
        found = find_in_path(name_or_path)
        if found is None:
            raise SandboxError(f"no se encontró {name_or_path!r} en el PATH; ¿está instalado Claude Code?")
        candidate = found
    exe = candidate.resolve()
    root = workspace_root.resolve()
    if exe.is_relative_to(root):
        raise SandboxError(f"el ejecutable no puede estar dentro de la carpeta de trabajo: {exe}")
    return exe


def find_in_path(name: str, env: Mapping[str, str] | None = None, windows: bool | None = None) -> Path | None:
    """Busca un ejecutable solo en las entradas absolutas del PATH.

    No usa shutil.which: en Windows, which puede mirar primero el directorio
    actual, y un ``claude.cmd`` dejado ahí (p. ej. en Descargas) le ganaría al
    real. Las entradas relativas del PATH también dependen del directorio
    actual, así que se ignoran en todas las plataformas.
    """
    env = os.environ if env is None else env
    windows = os.name == "nt" if windows is None else windows
    names = [name]
    if windows and not Path(name).suffix:
        exts = [e for e in env.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        names = [name + ext for ext in exts]
    for entry in env.get("PATH", "").split(os.pathsep):
        directory = Path(entry.strip().strip('"'))
        if not entry.strip() or not directory.is_absolute():
            continue
        for candidate_name in names:
            candidate = directory / candidate_name
            if candidate.is_file() and (windows or os.access(candidate, os.X_OK)):
                return candidate
    return None


def _resolve_dir(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):  # no existe, o bucle de symlinks
        raise SandboxError(f"{label} no existe: {path}") from None
    if not resolved.is_dir():
        raise SandboxError(f"{label} no es una carpeta: {path}")
    return resolved
