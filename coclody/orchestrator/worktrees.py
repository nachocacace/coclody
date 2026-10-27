"""Worktrees de los workers: CoClody los valida y crea los que falten, nunca borra ninguno.

Disposición, siempre dentro de la carpeta raíz del proyecto activo:

    <raíz>/.coclody/worktrees/w1   rama coclody/w1
    <raíz>/.coclody/worktrees/w2   rama coclody/w2
    ...

Cada worker trabaja solo en su worktree (su carpeta de trabajo es esa, y sus
herramientas de archivos están acotadas a ``./**``). Las rutas de los
worktrees son hermanas, nunca una dentro de otra, así el alcance de un worker
no incluye el de otro.

Validaciones:
- la raíz está dentro de ``claude.workspace_root`` (la misma frontera de hoy),
  es la carpeta principal de un repo de git (no un worktree secundario) y
  está en una rama (no en detached HEAD); esa rama es la base de los workers;
- ninguna rama de worker es ``main``, ``master`` ni la rama base;
- ``.coclody/worktrees`` es una carpeta real dentro de la raíz (no un symlink
  que la saque de ahí), y cada worktree que ya existe es un worktree de este
  mismo repo, en la rama que le corresponde.

``.coclody/`` se agrega a ``.git/info/exclude`` (local, no versionado) para que
los worktrees no aparezcan como archivos sin seguimiento en la raíz.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from coclody.orchestrator.gitops import Git, GitError

WORKTREES_REL = PurePosixPath(".coclody", "worktrees")
BRANCH_PREFIX = "coclody/"
PROTECTED_BRANCHES = frozenset({"main", "master"})
EXCLUDE_LINE = "/.coclody/"


class WorktreeError(Exception):
    pass


@dataclass(frozen=True)
class WorkerSpec:
    name: str
    branch: str
    path_rel: str  # relativa a la raíz del proyecto, con "/"

    def path(self, root: Path) -> Path:
        return root.joinpath(*PurePosixPath(self.path_rel).parts)


@dataclass(frozen=True)
class WorktreeReport:
    spec: WorkerSpec
    status: str  # "ok" | "created" | "missing" | "error"
    problem: str | None = None


def worker_specs(count: int) -> list[WorkerSpec]:
    return [WorkerSpec(f"w{i}", f"{BRANCH_PREFIX}w{i}", str(WORKTREES_REL / f"w{i}")) for i in range(1, count + 1)]


def validate_root(git: Git, root: Path, workspace_root: Path) -> tuple[Path, str]:
    """(raíz resuelta, rama base). Lanza WorktreeError si la raíz no sirve para un proyecto."""
    if not root.is_absolute():
        raise WorktreeError("la carpeta raíz tiene que ser una ruta absoluta")
    try:
        resolved = root.resolve(strict=True)
    except (OSError, RuntimeError):
        raise WorktreeError(f"la carpeta raíz no existe: {root}") from None
    if not resolved.is_dir():
        raise WorktreeError(f"la carpeta raíz no es una carpeta: {root}")
    workspace = workspace_root.resolve()
    if not resolved.is_relative_to(workspace):
        raise WorktreeError(f"la carpeta raíz tiene que estar dentro de la carpeta de trabajo ({workspace})")
    try:
        toplevel = git.toplevel(resolved)
        entries = git.worktrees(resolved)
    except GitError as exc:
        raise WorktreeError(f"la carpeta raíz no es un repo de git: {exc}") from None
    if toplevel != resolved:
        raise WorktreeError(f"la carpeta raíz tiene que ser la raíz del repo ({toplevel}), no una subcarpeta")
    if not entries or entries[0].path != resolved:
        raise WorktreeError("la carpeta raíz es un worktree secundario; usá la carpeta principal del repo")
    base = git.current_branch(resolved)
    if base is None:
        raise WorktreeError("la carpeta raíz está en detached HEAD; poné una rama (será la base de los workers)")
    if base.startswith(BRANCH_PREFIX):
        raise WorktreeError(f"la carpeta raíz está en {base!r}, una rama de worker; volvé a tu rama principal")
    return resolved, base


def ensure_worktrees(git: Git, root: Path, base_branch: str, specs: list[WorkerSpec], *,
                     create: bool) -> list[WorktreeReport]:
    """Valida cada worktree y, con ``create``, crea los que falten. Nunca borra ni mueve nada."""
    protected = PROTECTED_BRANCHES | {base_branch}
    container = root.joinpath(*WORKTREES_REL.parts)
    # Antes de crear nada: si .coclody fuera un symlink, mkdir lo seguiría y crearía carpetas afuera.
    container_problem = _container_problem(root, container)
    if create and not container_problem:
        _ensure_exclude(git, root)
        container.mkdir(parents=True, exist_ok=True)
        container_problem = _container_problem(root, container)
    listed = {e.path: e for e in git.worktrees(root)}
    reports = []
    for spec in specs:
        if spec.branch in protected:
            reports.append(WorktreeReport(spec, "error", f"la rama {spec.branch!r} está protegida"))
            continue
        if container_problem:
            reports.append(WorktreeReport(spec, "error", container_problem))
            continue
        path = spec.path(root)
        if path.is_symlink():
            reports.append(WorktreeReport(spec, "error", "es un symlink; tiene que ser una carpeta real"))
            continue
        if path.exists():
            reports.append(WorktreeReport(spec, *_check_existing(path, spec, listed)))
            continue
        if not create:
            reports.append(WorktreeReport(spec, "missing", "no existe; se crea al activar el proyecto o al arrancar"))
            continue
        try:
            base = None if git.branch_exists(root, spec.branch) else base_branch
            git.add_worktree(root, path, spec.branch, base)
        except GitError as exc:
            reports.append(WorktreeReport(spec, "error", f"no se pudo crear: {exc}"))
            continue
        listed = {e.path: e for e in git.worktrees(root)}
        status, problem = _check_existing(path, spec, listed)
        reports.append(WorktreeReport(spec, "created" if status == "ok" else status, problem))
    return reports


def _check_existing(path: Path, spec: WorkerSpec, listed) -> tuple[str, str | None]:
    entry = listed.get(path.resolve())
    if entry is None:
        return "error", "la carpeta existe pero no es un worktree de este repo"
    if entry.branch != spec.branch:
        return "error", f"está en {entry.branch or 'detached HEAD'!r} y tiene que estar en {spec.branch!r}"
    return "ok", None


def _container_problem(root: Path, container: Path) -> str | None:
    for part in (root / ".coclody", container):
        if part.is_symlink():
            return f"{part.relative_to(root).as_posix()} es un symlink; tiene que ser una carpeta real dentro del proyecto"
        if part.exists() and not part.is_dir():
            return f"{part.relative_to(root).as_posix()} existe y no es una carpeta"
    if container.exists() and not container.resolve().is_relative_to(root):
        return ".coclody/worktrees apunta fuera de la carpeta del proyecto"
    return None


def _ensure_exclude(git: Git, root: Path) -> None:
    exclude = git.common_dir(root) / "info" / "exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    current = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if EXCLUDE_LINE not in current.splitlines():
        with exclude.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(("" if current.endswith("\n") or not current else "\n") + EXCLUDE_LINE + "\n")
