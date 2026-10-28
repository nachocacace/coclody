"""Workers del orquestador: cada uno ejecuta las tareas de su cola en su propio worktree.

Dispatch mecánico: un worker libre toma la próxima tarea en cola asignada a
él, sin invocar al cerebro.

Por tarea:

1. Antes: se toma una foto de git (HEAD y rama del worktree, ramas del repo,
   lista de worktrees) y se guarda el HEAD en una ref de respaldo
   (``refs/coclody/backup/<worker>/t<tarea>-r<run>``).
2. Claude Code corre con su carpeta de trabajo en el worktree, herramientas de
   archivos acotadas a ``./**`` y, si la config los tiene, ``Bash(<cmd>:*)``
   solo para los ``worker_commands``. Nunca git.
3. Después, CoClody (no el worker) commitea todo lo que cambió en la rama del
   worker, sin hooks, y verifica las invariantes: el worktree sigue en su
   rama, el HEAD no se movió por fuera de CoClody, ninguna rama que no sea de
   un worker cambió ni desapareció, y la lista de worktrees es la misma.
   Si una invariante no se cumple, la tarea falla, el worker queda en estado
   ``error`` (no toma más tareas hasta reiniciar) y el aviso lo explica.

Entorno de Claude Code (y de lo que corra con Bash), por mitigación: sin
agente SSH, sin variables con pinta de secreto (``*TOKEN*``, ``*SECRET*``...),
sin la config global ni la del sistema de git (no hay helpers de
credenciales), y con hooks desactivados y ``protocol.allow=never`` inyectados
por variables de entorno. Esto NO es una garantía: código que corra con Bash
(tests, build) puede rehacer cualquiera de esas cosas. Lo garantizado por
construcción sin ``worker_commands`` está en el README.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from coclody.config import WORKSPACE_SCOPE, Config
from coclody.db import DbFactory, repo
from coclody.db import projects as prepo
from coclody.db.repo import Question, Task
from coclody.orchestrator.claude_runner import ClaudeResult, build_invocation, run_claude
from coclody.orchestrator.gitops import Git, GitError
from coclody.orchestrator.sandbox import SandboxError, resolve_executable
from coclody.orchestrator.sensitive import classify
from coclody.orchestrator.worker import CancelRegistry, Runner, Worker, _failure, build_prompt
from coclody.orchestrator.worktrees import BRANCH_PREFIX, WorkerSpec

log = logging.getLogger(__name__)

WORKER_FILE_TOOLS = tuple(f"{tool}{WORKSPACE_SCOPE}" for tool in ("Read", "Glob", "Grep", "Edit", "Write"))
# Reglas de denegación: ganan a lo preaprobado. En un worktree, .git es un archivo puntero; si el
# worker lo edita (o escribe core.hooksPath en el repo), ejecuta código cuando CoClody corra git.
# .gitattributes y .gitmodules tambien pueden desviar a git. Se deniegan por construccion.
_DENY_PATHS = (".git", ".git/**", "**/.git", "**/.git/**", ".gitattributes", "**/.gitattributes",
               ".gitmodules", "**/.gitmodules")
_WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
DENY_TOOLS = tuple(f"{tool}({path})" for tool in _WRITE_TOOLS for path in _DENY_PATHS)
_SECRET_NAME_RE = re.compile(r"TOKEN|SECRET|PASSWORD|PASSWD|API_?KEY|ACCESS_?KEY|CREDENTIAL|PRIVATE_?KEY", re.I)
# Claude Code puede autenticarse con esta variable: si se la quitara, no arrancaría.
_KEEP = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
_DROP = frozenset({"SSH_AUTH_SOCK", "SSH_AGENT_PID", "SSH_ASKPASS", "GIT_ASKPASS", "DISPLAY"})
COMMIT_SUMMARY_CHARS = 3000

WORKER_RULES = """\
Reglas de este worker (CoClody, modo orquestador):
- Sos el worker {worker} del proyecto {project}. Tu carpeta de trabajo es tu worktree de git, en la rama
  {branch}. No toques nada fuera de esta carpeta.
- No uses git para nada: al terminar, CoClody commitea tus cambios en tu rama. El merge lo hace una persona.
{commands}- Al terminar, cerrá tu respuesta con un bloque que empiece con la línea "RESUMEN" y diga en pocas líneas
  qué hiciste, qué archivos tocaste, qué verificaste y qué queda pendiente.
"""


def worker_tools(commands: tuple[str, ...]) -> tuple[str, ...]:
    return WORKER_FILE_TOOLS + tuple(f"Bash({command}:*)" for command in commands)


def worker_env(base: Mapping[str, str], hooks_dir: Path, empty_config: Path) -> dict[str, str]:
    env = {
        k: v for k, v in base.items()
        if k in _KEEP or not (k.upper() in _DROP or k.upper().startswith("GIT_") or _SECRET_NAME_RE.search(k))
    }
    overrides = [
        ("core.hooksPath", str(hooks_dir)),
        ("protocol.allow", "never"),
        ("credential.helper", ""),
        ("core.sshCommand", "false"),
        ("core.fsmonitor", "false"),
    ]
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(empty_config),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": str(len(overrides)),
    })
    for i, (key, value) in enumerate(overrides):
        env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
    return env


def sandbox_available() -> bool:
    """El sandbox de Bash de Claude Code existe en macOS (Seatbelt) y Linux (bubblewrap), no en Windows."""
    return sys.platform in ("darwin", "linux")


def sandbox_settings(root: Path, worktree: Path) -> str:
    """--settings para confinar lo que corra con Bash: solo lee/escribe el worktree, sin red.

    Verificado con Claude Code real (macOS): con esto, código lanzado por Bash no pudo leer el .env de
    la raíz ni escribir el .git común, el worktree vecino o fuera de la carpeta. failIfUnavailable hace
    que, si el sandbox no puede confinar, el comando falle en vez de correr suelto (cierra ante el error).
    El home queda denegado por defecto (SSH, llavero, datos de CoClody) y solo se reabre el worktree.
    """
    settings = {
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False,
            "network": {"allowedDomains": []},
            "filesystem": {"denyRead": [str(root)], "allowRead": [str(worktree)]},
        }
    }
    return json.dumps(settings)


@dataclass(frozen=True)
class GitSnapshot:
    head: str
    branch: str | None
    others: dict[str, str]  # ramas que no son de workers → commit
    worktrees: frozenset[tuple[str, str | None]]


@dataclass(frozen=True)
class CommitInfo:
    base_sha: str
    head_sha: str
    files: int
    insertions: int
    deletions: int
    big_change: bool
    sensitive: tuple[str, ...] = ()


class InvariantError(Exception):
    pass


class WorktreeWorker(Worker):
    """Un worker del orquestador atado a un worktree. Una instancia por (proyecto, worker)."""

    def __init__(self, config: Config, db: DbFactory, cancels: CancelRegistry, git: Git, project: prepo.Project,
                 spec: WorkerSpec, hooks_dir: Path, empty_git_config: Path, runner: Runner = run_claude):
        super().__init__(config.claude, db, cancels, runner, max_task_runs=config.orchestrator.max_task_runs)
        self.config, self.git, self.project, self.spec = config, git, project, spec
        self.root = project.root_path
        self.worktree = spec.path(self.root)
        self.hooks_dir, self.empty_git_config = hooks_dir, empty_git_config
        self._commit: CommitInfo | None = None
        self._violation: str | None = None

    # --- puntos de extensión de Worker -------------------------------------------------------------

    def _next_task(self, conn) -> Task | None:
        return repo.next_queued_task_for(conn, self.project.id, self.spec.name)

    def _prompt(self, task: Task, answered: list[Question], nonce: str) -> str:
        commands = self.config.orchestrator.worker_commands
        listed = (f"- Para verificar podés correr solo estos comandos: {', '.join(commands)}.\n" if commands
                  else "- No podés correr comandos: solo leer y editar archivos.\n")
        rules = WORKER_RULES.format(worker=self.spec.name, project=self.project.name, branch=self.spec.branch,
                                    commands=listed)
        return build_prompt(task, answered, nonce) + "\n" + rules

    def _execute(self, task: Task, prompt: str, resume: str | None, cancel) -> ClaudeResult:
        self._commit, self._violation = None, None
        with self.db() as conn:  # acá la tarea ya pasó a running
            prepo.set_worker_state(conn, self.project.id, self.spec.name, "running", task.id)
        try:
            before = self._snapshot()
            self.git.backup_ref(self.root, f"{self.spec.name}/t{task.id}-{time.time_ns()}", before.head)
        except InvariantError as exc:
            self._violation = f"Invariante de git violada en {self.spec.name} antes de empezar: {exc}"
            log.error("Tarea %s: %s", task.id, self._violation)
            return _failure(self._violation)
        except GitError as exc:
            return _failure(f"No se pudo preparar el worktree {self.spec.name}: {exc}")
        try:
            # El ejecutable se verifica contra la carpeta de trabajo general, no solo contra el worktree.
            resolve_executable(self.claude.executable, self.claude.workspace_root)
            commands = self.config.orchestrator.worker_commands
            tools = worker_tools(commands)
            cfg = dataclasses.replace(self.claude, workspace_root=self.worktree, allowed_tools=tools)
            env = worker_env(os.environ, self.hooks_dir, self.empty_git_config)
            # El sandbox solo hace falta cuando hay Bash: sin comandos, las herramientas de archivo ya
            # estan acotadas a ./** y no hay forma de ejecutar codigo. La config rechaza worker_commands
            # donde no hay sandbox, asi que si hay comandos, el sandbox esta disponible.
            settings = sandbox_settings(self.root, self.worktree) if commands else None
            inv = build_invocation(cfg, prompt, workdir_rel=None, resume_session=resume, base_env=env,
                                   deny_tools=DENY_TOOLS, settings_json=settings)
            result = self.runner(inv, cancel)
        except SandboxError as exc:
            result = _failure(f"No se pudo lanzar Claude Code: {exc}")
        except Exception as exc:  # cualquier otra falla igual tiene que cerrar el run, la tarea y verificar git
            log.exception("Falló la ejecución de la tarea %s en %s", task.id, self.spec.name)
            result = _failure(f"Error interno al ejecutar: {type(exc).__name__}: {exc}")
        return self._after(task, before, result)

    def _before_finish(self, conn, task: Task, run_id: int, result: ClaudeResult) -> None:
        if self._commit is not None:
            c = self._commit
            repo.add_task_commit(conn, task.id, run_id, self.project.id, self.spec.name, c.base_sha, c.head_sha,
                                 c.files, c.insertions, c.deletions, c.big_change, ",".join(c.sensitive))
        if self._violation is not None:
            repo.add_event(conn, task.id, "error", self._violation, run_id=run_id)
            prepo.set_worker_state(conn, self.project.id, self.spec.name, "error")
        else:
            prepo.set_worker_state(conn, self.project.id, self.spec.name, "idle")

    # --- git --------------------------------------------------------------------------------------------

    def _snapshot(self) -> GitSnapshot:
        branch = self.git.current_branch(self.worktree)
        if branch != self.spec.branch:
            raise InvariantError(f"el worktree está en {branch or 'detached HEAD'!r} y no en {self.spec.branch!r}")
        heads = self.git.branch_heads(self.root)
        others = {b: sha for b, sha in heads.items() if not b.startswith(BRANCH_PREFIX)}
        worktrees = frozenset((str(e.path), e.branch) for e in self.git.worktrees(self.root))
        return GitSnapshot(self.git.head(self.worktree), branch, others, worktrees)

    def _after(self, task: Task, before: GitSnapshot, result: ClaudeResult) -> ClaudeResult:
        """Verifica, commitea lo que haya cambiado y vuelve a verificar. Una violación hace fallar la tarea."""
        try:
            self._check(before, expected_head=before.head)
            if self.git.has_changes(self.worktree):
                head = self.git.commit_all(self.worktree, commit_message(task, self.spec.name, result))
                files, ins, dels = self.git.numstat(self.worktree, before.head, head)
                sensitive = tuple(classify(self.git.changed_files(self.worktree, before.head, head)))
                orch = self.config.orchestrator
                big = files > orch.big_change_files or ins + dels > orch.big_change_lines
                self._commit = CommitInfo(before.head, head, files, ins, dels, big, sensitive)
                self._check(before, expected_head=head)
        except InvariantError as exc:
            self._violation = f"Invariante de git violada en {self.spec.name}: {exc}"
            log.error("Tarea %s: %s", task.id, self._violation)
            return dataclasses.replace(result, status="error", summary=f"{self._violation}\n\n{result.summary}")
        except GitError as exc:
            message = f"No se pudo commitear en {self.spec.name}: {exc}"
            log.error("Tarea %s: %s", task.id, message)
            return dataclasses.replace(result, status="error", summary=f"{message}\n\n{result.summary}")
        return result

    def _check(self, before: GitSnapshot, expected_head: str) -> None:
        now = self._snapshot()
        if now.head != expected_head:
            raise InvariantError("el HEAD del worktree cambió por fuera de CoClody (¿alguien corrió git?)")
        changed = sorted(b for b, sha in before.others.items() if now.others.get(b) != sha)
        if changed:
            raise InvariantError(f"cambiaron o desaparecieron ramas que no son de workers: {', '.join(changed)}")
        if now.worktrees != before.worktrees:
            raise InvariantError("cambió la lista de worktrees del repo")


def commit_message(task: Task, worker: str, result: ClaudeResult) -> str:
    summary = _clean(result.result_text or result.summary)[:COMMIT_SUMMARY_CHARS]
    title = _clean(task.title).replace("\n", " ")[:100]
    return (f"coclody({worker}): {title}\n\n{summary}\n\n"
            f"CoClody-Task: {task.id}\nCoClody-Worker: {worker}\nCoClody-Status: {result.status}")


def _clean(text: str) -> str:
    # Sin caracteres de control (salvo saltos de línea y tabs): el texto viene de Claude.
    return "".join(ch for ch in text if ch in "\n\t" or ch >= " ").strip() or "(sin resumen)"
