"""El único lugar donde CoClody ejecuta git.

Garantías (por construcción, para cada llamada):

- Lista de argumentos fija armada acá, sin shell. Ni el cerebro ni los
  workers pueden pedir un comando de git: no hay ninguna función que reciba
  argumentos libres desde afuera de este módulo.
- Sin hooks: ``core.hooksPath`` apunta a una carpeta de CoClody que no existe o está vacía. Si no,
  un ``post-checkout`` o un pre-commit (configurado con un archivo que un
  worker puede escribir) ejecutaría código cuando CoClody crea un worktree o
  commitea. Si esa carpeta aparece con algo adentro, no se ejecuta git.
- Sin red: ``protocol.allow=never``. Ningún comando de CoClody habla con un
  remoto; con esto, aunque una configuración lo intentara, git se niega.
- Sin ``core.fsmonitor`` (otro programa externo que git puede lanzar), sin
  paginador y sin pedir credenciales por terminal.
- El entorno no hereda variables ``GIT_*`` (``GIT_DIR`` o ``GIT_WORK_TREE``
  podrían apuntar git a otro repo).
- El ejecutable de git se busca solo en las entradas absolutas del PATH y no
  puede estar dentro de la carpeta de trabajo (igual que el de Claude Code).
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from coclody.orchestrator.sandbox import SandboxError, resolve_executable

GIT_TIMEOUT_S = 60
_MAX_OUTPUT = 1_000_000


class GitError(Exception):
    pass


class _Exit(GitError):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class WorktreeEntry:
    path: Path
    head: str | None
    branch: str | None  # "refs/heads/..." recortado a nombre corto; None si está en detached HEAD


class Git:
    def __init__(self, executable: Path, hooks_dir: Path):
        self.executable = executable
        self.hooks_dir = hooks_dir

    @classmethod
    def find(cls, workspace_root: Path, hooks_dir: Path) -> Git:
        try:
            exe = resolve_executable("git", workspace_root)
        except SandboxError as exc:
            raise GitError(f"git: {exc}") from None
        return cls(exe, hooks_dir)

    # --- consultas ------------------------------------------------------------------------------

    def toplevel(self, path: Path) -> Path:
        return Path(self._run(path, "rev-parse", "--show-toplevel").strip()).resolve()

    def common_dir(self, path: Path) -> Path:
        return Path(self._run(path, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()).resolve()

    def current_branch(self, path: Path) -> str | None:
        """Rama actual, o None si está en detached HEAD."""
        try:
            return self._run(path, "symbolic-ref", "--quiet", "--short", "HEAD").strip() or None
        except GitError:
            return None

    def head(self, path: Path) -> str:
        return self._run(path, "rev-parse", "--verify", "HEAD^{commit}").strip()

    def branch_exists(self, repo: Path, branch: str) -> bool:
        self._check_branch_name(repo, branch)
        try:
            self._run(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}^{{commit}}")
        except GitError:
            return False
        return True

    def worktrees(self, repo: Path) -> list[WorktreeEntry]:
        out = self._run(repo, "worktree", "list", "--porcelain")
        entries, current = [], {}
        for line in out.splitlines() + [""]:
            if not line:
                if "worktree" in current:
                    branch = current.get("branch")
                    if branch and branch.startswith("refs/heads/"):
                        branch = branch[len("refs/heads/"):]
                    entries.append(WorktreeEntry(Path(current["worktree"]).resolve(), current.get("HEAD"), branch))
                current = {}
                continue
            key, _, value = line.partition(" ")
            current[key] = value
        return entries

    def branch_heads(self, repo: Path) -> dict[str, str]:
        """Rama → commit, de todas las ramas locales del repo."""
        out = self._run(repo, "for-each-ref", "--format=%(refname:short) %(objectname)", "refs/heads")
        return dict(line.rsplit(" ", 1) for line in out.splitlines() if " " in line)

    def is_ancestor(self, path: Path, older: str, newer: str) -> bool:
        return self._returncode(path, "merge-base", "--is-ancestor", older, newer) == 0

    def has_changes(self, path: Path) -> bool:
        return bool(self._run(path, "status", "--porcelain", "--untracked-files=all").strip())

    def numstat(self, path: Path, base: str, head: str) -> tuple[int, int, int]:
        """(archivos, líneas agregadas, líneas borradas) entre dos commits. Los binarios cuentan como archivo."""
        out = self._run(path, "diff", "--numstat", "--no-ext-diff", "--no-textconv", "--no-renames", base, head)
        files = insertions = deletions = 0
        for line in out.splitlines():
            added, removed, _ = (line.split("\t", 2) + ["", ""])[:3]
            files += 1
            insertions += int(added) if added.isdigit() else 0
            deletions += int(removed) if removed.isdigit() else 0
        return files, insertions, deletions

    # --- cambios (solo los que CoClody necesita) ---------------------------------------------------------

    def backup_ref(self, path: Path, name: str, sha: str) -> str:
        """Guarda ``sha`` en refs/coclody/backup/<name>: un reset o rebase posterior no lo pierde."""
        if not name or any(part in ("", ".", "..") for part in name.split("/")):
            raise GitError(f"nombre de respaldo inválido: {name!r}")
        ref = f"refs/coclody/backup/{name}"
        self._run(path, "check-ref-format", ref)
        self._run(path, "update-ref", ref, sha)
        return ref

    def commit_all(self, path: Path, message: str) -> str:
        """``git add -A`` y commit (sin hooks) en el worktree ``path``. Devuelve el commit nuevo."""
        self._run(path, "add", "--all", "--", ".")
        self._run(path, "commit", "--no-verify", "--quiet", "-m", message)
        return self.head(path)

    def add_worktree(self, repo: Path, path: Path, branch: str, base: str | None) -> None:
        """Crea un worktree en ``path``. Con ``base`` crea la rama ``branch`` desde ahí; sin ``base`` la usa."""
        self._check_branch_name(repo, branch)
        if base is None:
            self._run(repo, "worktree", "add", "--", str(path), branch)
        else:
            self._run(repo, "worktree", "add", "-b", branch, "--", str(path), base)

    # --- ejecución ---------------------------------------------------------------------------------

    def _check_branch_name(self, repo: Path, branch: str) -> None:
        if not isinstance(branch, str) or not branch or branch.startswith("-"):
            raise GitError(f"nombre de rama inválido: {branch!r}")
        self._run(repo, "check-ref-format", "--branch", branch)

    def _returncode(self, cwd: Path, *args: str) -> int:
        try:
            self._run(cwd, *args)
        except _Exit as exc:
            return exc.code
        return 0

    def _run(self, cwd: Path, *args: str) -> str:
        self._check_hooks_dir()
        argv = [
            str(self.executable),
            "--no-pager",
            "-c", f"core.hooksPath={self.hooks_dir}",
            "-c", "core.fsmonitor=false",
            "-c", "protocol.allow=never",
            "-C", str(cwd),
            *args,
        ]
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=git_env(),
                timeout=GIT_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0]} tardó más de {GIT_TIMEOUT_S} s") from None
        except OSError as exc:
            raise GitError(f"no se pudo ejecutar git: {exc}") from None
        if proc.returncode != 0:
            detail = proc.stderr[:2000].decode("utf-8", "replace").strip()
            raise _Exit(proc.returncode, f"git {args[0]} falló ({proc.returncode}): {detail}")
        return proc.stdout[:_MAX_OUTPUT].decode("utf-8", "replace")

    def _check_hooks_dir(self) -> None:
        # Si no existe, git tampoco encuentra hooks: no hace falta crearla (y check no deja nada en disco).
        if self.hooks_dir.is_symlink() or (self.hooks_dir.exists() and not self.hooks_dir.is_dir()):
            raise GitError(f"la carpeta de hooks de CoClody no es una carpeta real ({self.hooks_dir}); no se ejecuta git")
        if self.hooks_dir.is_dir() and any(self.hooks_dir.iterdir()):
            raise GitError(f"la carpeta de hooks vacía de CoClody tiene archivos ({self.hooks_dir}); no se ejecuta git")


def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    env.update({"GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C", "LANG": "C"})
    return env
