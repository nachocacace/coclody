"""Lógica de los comandos del bot, independiente de Telegram.

Cada método recibe datos ya extraídos del mensaje (chat ID, texto) y devuelve
el texto de la respuesta. Corre en un hilo aparte (``asyncio.to_thread``) y
abre su propia conexión a la base.

La autorización ya pasó antes de llegar acá (ChatGuard): todo chat que llega
es un chat privado de la lista blanca, y la lista blanca es de confianza
total (cualquiera de esos chats puede ver, responder y cancelar cualquier tarea).
"""

from __future__ import annotations

import re

from coclody.db import DbFactory, repo
from coclody.db.repo import InvalidTransitionError, Question, RepoError, Task
from coclody.orchestrator.worker import CANCEL_USER, CancelRegistry

TELEGRAM_MAX_CHARS = 4096
ACTIVE_STATUSES = ("pending", "queued", "running", "waiting_input")
FINISHED_STATUSES = ("done", "failed", "cancelled")
CANCELLABLE_STATUSES = frozenset({"pending", "queued", "waiting_input"})

STATUS_LABELS = {
    "pending": "pendiente",
    "queued": "en cola",
    "running": "ejecutando",
    "waiting_input": "esperando respuesta",
    "done": "terminada",
    "failed": "falló",
    "cancelled": "cancelada",
}

HELP = """\
CoClody: tareas para Claude Code.

/lista: tareas activas y las últimas terminadas
/nueva <título>: crea una tarea y la pone en cola. Las líneas siguientes al título son la descripción.
/estado <id>: detalle de una tarea
/responder <id> <texto>: responde la pregunta pendiente de una tarea. También podés responder directamente al mensaje de la pregunta.
/cancelar <id>: cancela una tarea
/reintentar <id>: vuelve a poner en cola una tarea que falló
/ayuda: esta ayuda"""

_ID_RE = re.compile(r"#?([1-9][0-9]{0,15})")


def clip(text: str, limit: int = TELEGRAM_MAX_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def parse_task_id(raw: str) -> int | None:
    match = _ID_RE.fullmatch(raw.strip())
    return int(match.group(1)) if match else None


def label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


class Commands:
    def __init__(self, db: DbFactory, cancels: CancelRegistry):
        self._db = db
        self._cancels = cancels

    def help(self) -> str:
        return HELP

    def list_tasks(self) -> str:
        with self._db() as conn:
            active = repo.list_tasks(conn, statuses=ACTIVE_STATUSES, limit=20)
            finished = repo.list_tasks(conn, statuses=FINISHED_STATUSES, limit=5)
        if not active and not finished:
            return "No hay tareas. Creá una con /nueva <título>."
        parts = []
        if active:
            parts.append("Activas:\n" + "\n".join(_line(t) for t in active))
        else:
            parts.append("No hay tareas activas.")
        if finished:
            parts.append("Últimas terminadas:\n" + "\n".join(_line(t) for t in finished))
        return "\n\n".join(parts)

    def new_task(self, chat_id: int, text: str) -> str:
        title, _, description = text.strip().partition("\n")
        if not title.strip():
            return "Uso: /nueva <título>\n(las líneas siguientes son la descripción, opcional)"
        try:
            with self._db() as conn:
                task = repo.create_task(conn, title, description=description or None, created_by=chat_id)
                task = repo.transition_task(conn, task.id, "queued", expected="pending")
        except RepoError as exc:
            return f"No pude crear la tarea: {exc}"
        return f"Creé la tarea #{task.id}: {task.title}\nQuedó en cola."

    def status(self, arg: str) -> str:
        task_id = parse_task_id(arg)
        if task_id is None:
            return "Uso: /estado <id>"
        with self._db() as conn:
            task = repo.get_task(conn, task_id)
            if task is None:
                return f"No existe la tarea #{task_id}."
            open_questions = repo.list_open_questions(conn, task_id=task.id)
            runs = repo.list_runs(conn, task.id)
            events = repo.list_events(conn, task.id, limit=1000)[-5:]
        lines = [
            f"#{task.id} {task.title}",
            f"Estado: {label(task.status)}",
            f"Carpeta: {task.workdir_rel or '(raíz de trabajo)'}",
            f"Creada: {task.created_at} UTC · actualizada: {task.updated_at} UTC",
        ]
        if task.description:
            lines.append(f"\nDescripción:\n{clip(task.description, 1000)}")
        for q in open_questions:
            lines.append(f"\nPregunta pendiente:\n{q.question}\n(respondé con /responder {task.id} <texto>)")
        if runs:
            last = runs[-1]
            lines.append(f"\nÚltima ejecución ({len(runs)} en total): {last.status}, {last.started_at} UTC")
            if last.summary:
                lines.append(clip(last.summary, 1500))
        if events:
            lines.append("\nÚltimos eventos:")
            lines += [f"- {e.created_at} {e.kind}: {clip(e.message, 200)}" for e in events]
        return clip("\n".join(lines))

    def answer(self, chat_id: int, text: str) -> str:
        """``/responder <id> <texto>``: responde la pregunta abierta más vieja de la tarea."""
        parts = text.strip().split(maxsplit=1)
        task_id = parse_task_id(parts[0]) if parts else None
        answer = parts[1] if len(parts) == 2 else ""
        if task_id is None or not answer.strip():
            return "Uso: /responder <id> <texto>"
        with self._db() as conn:
            task = repo.get_task(conn, task_id)
            if task is None:
                return f"No existe la tarea #{task_id}."
            open_questions = repo.list_open_questions(conn, task_id=task.id)
            if not open_questions:
                return f"La tarea #{task.id} no tiene preguntas pendientes."
            return self._answer(conn, open_questions[0], answer, chat_id)

    def answer_reply(self, chat_id: int, reply_to_message_id: int, text: str) -> str | None:
        """Respuesta directa al mensaje de una pregunta. None si ese mensaje no es una pregunta."""
        with self._db() as conn:
            question = repo.get_question_by_message(conn, chat_id, reply_to_message_id)
            if question is None:
                return None
            if question.status != "open":
                return "Esa pregunta ya no está pendiente."
            return self._answer(conn, question, text, chat_id)

    def unmatched_text(self, chat_id: int) -> str:
        """Texto que no es un comando ni un reply a una pregunta.

        Nunca se lo toma como respuesta por adivinación: si hay preguntas
        pendientes, se explica cómo responderlas. Pasa, por ejemplo, al
        contestar desde la notificación del celular: Telegram manda un mensaje
        común, no un reply al mensaje de la pregunta.
        """
        with self._db() as conn:
            pending = repo.open_questions_sent_to(conn, chat_id)
        if not pending:
            return "No entendí. Mandá /ayuda para ver los comandos."
        tasks = ", ".join(f"#{q.task_id}" for q in pending)
        example = pending[-1].task_id
        return (
            "Tu mensaje no llegó como respuesta a una pregunta (Telegram no lo marcó como reply; "
            "pasa, por ejemplo, al contestar desde la notificación).\n"
            f"Preguntas pendientes: tarea {tasks}.\n"
            "Para responder: mantené apretado el mensaje de la pregunta → Responder, "
            f"o mandá /responder {example} <tu respuesta>."
        )

    def cancel(self, arg: str) -> str:
        task_id = parse_task_id(arg)
        if task_id is None:
            return "Uso: /cancelar <id>"
        # Dos intentos: el worker puede tomar la tarea justo entre la lectura y el cambio.
        for _ in range(2):
            with self._db() as conn:
                task = repo.get_task(conn, task_id)
                if task is None:
                    return f"No existe la tarea #{task_id}."
                if task.status == "running":
                    if self._cancels.request(task.id, CANCEL_USER):
                        return f"Cancelando la tarea #{task.id}…"
                    return f"La tarea #{task.id} está terminando; revisá /estado {task.id} en unos segundos."
                if task.status not in CANCELLABLE_STATUSES:
                    return f"La tarea #{task.id} ya terminó ({label(task.status)})."
                try:
                    repo.transition_task(conn, task.id, "cancelled", expected=task.status)
                except InvalidTransitionError:
                    continue
                for q in repo.list_open_questions(conn, task_id=task.id):
                    repo.expire_question(conn, q.id)
                return f"Cancelé la tarea #{task.id}."
        return f"La tarea #{task_id} cambió de estado mientras la cancelaba; probá de nuevo."

    def retry(self, arg: str) -> str:
        """Vuelve a encolar una tarea fallida (failed → queued, según TASK_TRANSITIONS)."""
        task_id = parse_task_id(arg)
        if task_id is None:
            return "Uso: /reintentar <id>"
        with self._db() as conn:
            task = repo.get_task(conn, task_id)
            if task is None:
                return f"No existe la tarea #{task_id}."
            if task.status != "failed":
                return f"Solo se reintentan tareas que fallaron; la #{task.id} está {label(task.status)}."
            try:
                repo.transition_task(conn, task.id, "queued", expected="failed")
            except InvalidTransitionError:
                return f"La tarea #{task.id} cambió de estado; revisá /estado {task.id}."
        return f"La tarea #{task.id} volvió a la cola."

    def _answer(self, conn, question: Question, text: str, chat_id: int) -> str:
        try:
            repo.answer_question(conn, question.id, text, answered_by=chat_id)
        except RepoError as exc:
            return f"No pude registrar la respuesta: {exc}"
        task = repo.get_task(conn, question.task_id)
        remaining = repo.list_open_questions(conn, task_id=task.id)
        if remaining:
            return f"Respuesta registrada. La tarea #{task.id} tiene {len(remaining)} pregunta(s) más pendiente(s)."
        if task.status == "waiting_input":
            try:
                repo.transition_task(conn, task.id, "queued", expected="waiting_input")
            except InvalidTransitionError:
                pass
            else:
                return f"Respuesta registrada. La tarea #{task.id} vuelve a la cola."
        return f"Respuesta registrada (la tarea #{task.id} está {label(task.status)})."


def _line(task: Task) -> str:
    return f"#{task.id} [{label(task.status)}] {clip(task.title, 80)}"
