"""Une la base con Claude Code: toma la próxima tarea en cola y la ejecuta.

Ciclo de una tarea:

    queued → running → (run: start → Claude Code → finish) → done | failed | waiting_input | cancelled

- Se ejecuta una sola tarea a la vez.
- Si Claude Code necesita un dato, termina su respuesta con una línea
  ``COCLODY_PREGUNTA: ...``; la tarea pasa a ``waiting_input`` con esa
  pregunta, y al responderla vuelve a la cola.
- Cada paso queda en la base (run + eventos). Si CoClody se corta a mitad de
  camino, ``recover()`` al arrancar cierra lo que quedó colgado.
- La cancelación es cooperativa: el bot pide cancelar en ``CancelRegistry`` y
  el worker mata el proceso y deja la tarea en ``cancelled``.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from coclody.config import ClaudeConfig
from coclody.db import DbFactory, repo
from coclody.db.repo import InvalidTransitionError, Question, Task
from coclody.orchestrator.claude_runner import ClaudeResult, Invocation, build_invocation, run_claude
from coclody.orchestrator.sandbox import SandboxError

log = logging.getLogger(__name__)

QUESTION_MARKER = "COCLODY_PREGUNTA:"
CANCEL_USER = "user"
CANCEL_SHUTDOWN = "shutdown"

PROMPT_TEMPLATE = """\
Estás ejecutando una tarea asignada a través de CoClody. Nadie está mirando esta sesión en vivo.

Tarea #{id}: {title}
{description}{answers}
Instrucciones:
- Trabajá solo dentro de la carpeta actual.
- Si para avanzar necesitás un dato que no tenés y no podés deducir, no lo inventes: terminá tu
  respuesta con una única línea final que empiece exactamente con "{marker}" seguida de la pregunta.
- Si terminaste, resumí en pocas líneas qué hiciste.
"""


class CancelRegistry:
    """Pedidos de cancelación para las tareas que se están ejecutando en este proceso."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._active: dict[int, tuple[threading.Event, list[str]]] = {}

    def register(self, task_id: int) -> threading.Event:
        with self._lock:
            event = threading.Event()
            self._active[task_id] = (event, [])
            return event

    def unregister(self, task_id: int) -> None:
        with self._lock:
            self._active.pop(task_id, None)

    def request(self, task_id: int, reason: str = CANCEL_USER) -> bool:
        """Pide cancelar. Devuelve False si la tarea no se está ejecutando acá."""
        with self._lock:
            entry = self._active.get(task_id)
            if entry is None:
                return False
            entry[1].append(reason)
            entry[0].set()
            return True

    def reason(self, task_id: int) -> str | None:
        with self._lock:
            entry = self._active.get(task_id)
            return entry[1][0] if entry and entry[1] else None

    def cancel_all(self, reason: str) -> int:
        with self._lock:
            for event, reasons in self._active.values():
                reasons.append(reason)
                event.set()
            return len(self._active)


Runner = Callable[[Invocation, threading.Event], ClaudeResult]


@dataclass
class Worker:
    claude: ClaudeConfig
    db: DbFactory
    cancels: CancelRegistry = field(default_factory=CancelRegistry)
    runner: Runner = run_claude

    def recover(self) -> int:
        """Cierra lo que quedó a medias si CoClody se cortó. Devuelve cuántas tareas tocó."""
        touched = 0
        with self.db() as conn:
            for run in repo.list_active_runs(conn):
                repo.finish_run(conn, run.id, "killed", summary="CoClody se cerró durante la ejecución.")
            for task in repo.list_tasks(conn, statuses=["running"], limit=1000):
                repo.add_event(conn, task.id, "error", "CoClody se cerró durante la ejecución.")
                repo.transition_task(conn, task.id, "failed", expected="running")
                touched += 1
        return touched

    def run_next(self) -> Task | None:
        """Ejecuta la próxima tarea en cola. Devuelve la tarea final o None si no había."""
        with self.db() as conn:
            task = repo.next_queued_task(conn)
            if task is None:
                return None
            # Se registra antes de pasar a running: una cancelación nunca cae en un hueco.
            cancel = self.cancels.register(task.id)
            try:
                task = repo.transition_task(conn, task.id, "running", expected="queued")
            except InvalidTransitionError:
                self.cancels.unregister(task.id)
                return None  # la cancelaron o editaron justo ahora
            run = repo.start_run(conn, task.id)
            resume = _last_session(conn, task.id, exclude_run=run.id)
            prompt = build_prompt(task, repo.list_questions(conn, task.id, status="answered"))

        try:
            result = self._execute(task, prompt, resume, cancel)
            reason = self.cancels.reason(task.id)
        finally:
            self.cancels.unregister(task.id)

        with self.db() as conn:
            return self._finish(conn, task, run.id, result, reason)

    def _execute(self, task: Task, prompt: str, resume: str | None, cancel: threading.Event) -> ClaudeResult:
        try:
            inv = build_invocation(self.claude, prompt, workdir_rel=task.workdir_rel, resume_session=resume)
            return self.runner(inv, cancel)
        except SandboxError as exc:
            return _failure(f"No se pudo lanzar Claude Code: {exc}")
        except Exception as exc:  # cualquier otra falla igual tiene que cerrar el run y la tarea
            log.exception("Falló la ejecución de la tarea %s", task.id)
            return _failure(f"Error interno al ejecutar: {type(exc).__name__}: {exc}")

    def _finish(self, conn, task: Task, run_id: int, result: ClaudeResult, reason: str | None) -> Task:
        if result.session_id:
            repo.set_run_session(conn, run_id, result.session_id)
        repo.finish_run(conn, run_id, result.status, exit_code=result.exit_code, summary=result.summary)

        if result.status == "killed":
            if reason == CANCEL_USER:
                for q in repo.list_open_questions(conn, task_id=task.id):
                    repo.expire_question(conn, q.id)
                return repo.transition_task(conn, task.id, "cancelled", expected="running")
            repo.add_event(conn, task.id, "error", "Interrumpida al cerrar CoClody.", run_id=run_id)
            return repo.transition_task(conn, task.id, "failed", expected="running")

        if result.status != "ok":
            repo.add_event(conn, task.id, "error", result.summary, run_id=run_id)
            return repo.transition_task(conn, task.id, "failed", expected="running")

        question = extract_question(result.result_text)
        if question:
            repo.add_event(conn, task.id, "question", question, run_id=run_id)
            repo.ask_question(conn, task.id, question)
            return repo.transition_task(conn, task.id, "waiting_input", expected="running")
        repo.add_event(conn, task.id, "progress", result.summary, run_id=run_id)
        return repo.transition_task(conn, task.id, "done", expected="running")


def build_prompt(task: Task, answered: list[Question]) -> str:
    answers = ""
    if answered:
        lines = "\n".join(f"- Pregunta: {q.question}\n  Respuesta: {q.answer}" for q in answered)
        answers = f"\nRespuestas del usuario a tus preguntas anteriores:\n{lines}\n"
    description = f"\n{task.description}\n" if task.description else ""
    return PROMPT_TEMPLATE.format(
        id=task.id, title=task.title, description=description, answers=answers, marker=QUESTION_MARKER
    )


def extract_question(result_text: str | None) -> str | None:
    """La pregunta, si la última línea no vacía del resultado empieza con el marcador."""
    if not result_text:
        return None
    lines = [line.strip() for line in result_text.strip().splitlines() if line.strip()]
    if not lines or not lines[-1].startswith(QUESTION_MARKER):
        return None
    question = lines[-1][len(QUESTION_MARKER) :].strip()
    if not question:
        return None
    return question if len(question) <= 4000 else question[:3999] + "…"


def _failure(summary: str) -> ClaudeResult:
    return ClaudeResult(
        status="error",
        exit_code=None,
        session_id=None,
        result_text=None,
        summary=summary,
        stdout_truncated=False,
        duration_s=0.0,
    )


def _last_session(conn, task_id: int, exclude_run: int) -> str | None:
    for run in reversed(repo.list_runs(conn, task_id)):
        if run.id != exclude_run and run.session_id:
            return run.session_id
    return None
