"""Operaciones sobre tasks, questions, runs y task_events.

Reglas generales:
- Cada escritura corre en una transacción (``BEGIN IMMEDIATE``): la
  verificación del estado actual y el cambio son atómicos.
- Los cambios de estado de una tarea siguen ``TASK_TRANSITIONS``; cualquier
  otro salto es un error.
- Las entradas se validan acá además de en los CHECK del esquema, para dar
  mensajes claros antes de llegar a SQLite.
- Cada cambio de estado de una tarea registra un evento ``status_change`` en
  la misma transacción, para que el feed de avance nunca quede desfasado.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from coclody.db.connection import transaction

TASK_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"queued", "waiting_input", "cancelled"}),
    "waiting_input": frozenset({"pending", "queued", "cancelled"}),
    "queued": frozenset({"pending", "running", "cancelled"}),
    "running": frozenset({"done", "failed", "waiting_input", "cancelled"}),
    "failed": frozenset({"queued", "cancelled"}),
    "done": frozenset(),
    "cancelled": frozenset(),
}
TERMINAL_STATUSES = frozenset({"done", "failed", "cancelled"})
EDITABLE_STATUSES = frozenset({"pending", "waiting_input", "queued"})
RUN_END_STATUSES = frozenset({"ok", "error", "timeout", "killed"})
EVENT_KINDS = frozenset({"status_change", "progress", "question", "error"})
# Solo estos cambios de estado se notifican por Telegram.
NOTIFY_STATUSES = frozenset({"done", "failed", "waiting_input"})
EVENT_MAX_LEN = 4000


class RepoError(Exception):
    pass


class NotFoundError(RepoError):
    pass


class InvalidTransitionError(RepoError):
    pass


class ValidationError(RepoError, ValueError):
    pass


@dataclass(frozen=True)
class Task:
    id: int
    title: str
    description: str | None
    status: str
    priority: int
    workdir_rel: str | None
    created_by: int | None
    created_at: str
    updated_at: str
    finished_at: str | None


@dataclass(frozen=True)
class Question:
    id: int
    task_id: int
    question: str
    answer: str | None
    status: str
    tg_message_id: int | None
    asked_at: str
    answered_at: str | None
    answered_by: int | None
    tg_chat_id: int | None
    kind: str = "open"


@dataclass(frozen=True)
class Run:
    id: int
    task_id: int
    session_id: str | None
    started_at: str
    ended_at: str | None
    exit_code: int | None
    status: str
    summary: str | None


@dataclass(frozen=True)
class TaskEvent:
    id: int
    task_id: int
    run_id: int | None
    kind: str
    message: str
    created_at: str
    notified: int
    notify: int


# --- tasks --------------------------------------------------------------------


def create_task(
    conn: sqlite3.Connection,
    title: str,
    description: str | None = None,
    priority: int = 0,
    workdir_rel: str | None = None,
    created_by: int | None = None,
) -> Task:
    values = (
        _text(title, "title", 200),
        _text(description, "description", 10000, required=False),
        _int(priority, "priority", -100, 100),
        normalize_workdir_rel(workdir_rel),
        _opt_id(created_by, "created_by"),
    )
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO tasks (title, description, priority, workdir_rel, created_by) VALUES (?, ?, ?, ?, ?)",
            values,
        )
        return _get(conn, Task, "tasks", cur.lastrowid)


def get_task(conn: sqlite3.Connection, task_id: int) -> Task | None:
    return _find(conn, Task, "tasks", task_id)


def list_tasks(conn: sqlite3.Connection, statuses: Iterable[str] | None = None, limit: int = 50) -> list[Task]:
    limit = _int(limit, "limit", 1, 1000)
    if statuses is None:
        rows = conn.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))
    else:
        wanted = sorted(set(statuses))
        unknown = set(wanted) - TASK_TRANSITIONS.keys()
        if unknown:
            raise ValidationError(f"estados desconocidos: {', '.join(sorted(unknown))}")
        if not wanted:
            return []
        marks = ", ".join("?" * len(wanted))
        rows = conn.execute(
            f"SELECT * FROM tasks WHERE status IN ({marks}) ORDER BY id DESC LIMIT ?", (*wanted, limit)
        )
    return [Task(**dict(row)) for row in rows]


def next_queued_task(conn: sqlite3.Connection) -> Task | None:
    row = conn.execute(
        "SELECT * FROM tasks WHERE status = 'queued' ORDER BY priority DESC, id ASC LIMIT 1"
    ).fetchone()
    return Task(**dict(row)) if row else None


def update_task(
    conn: sqlite3.Connection,
    task_id: int,
    *,
    title: str | None = None,
    description: str | None = None,
    priority: int | None = None,
    workdir_rel: str | None = None,
) -> Task:
    """Edita campos de una tarea que todavía no arrancó. ``None`` deja el campo como está."""
    changes: dict[str, Any] = {}
    if title is not None:
        changes["title"] = _text(title, "title", 200)
    if description is not None:
        changes["description"] = _text(description, "description", 10000, required=False)
    if priority is not None:
        changes["priority"] = _int(priority, "priority", -100, 100)
    if workdir_rel is not None:
        changes["workdir_rel"] = normalize_workdir_rel(workdir_rel)
    with transaction(conn):
        task = _get(conn, Task, "tasks", task_id)
        if task.status not in EDITABLE_STATUSES:
            raise InvalidTransitionError(f"la tarea {task.id} está en '{task.status}' y no se puede editar")
        if changes:
            sets = ", ".join(f"{column} = ?" for column in changes)
            conn.execute(
                f"UPDATE tasks SET {sets}, updated_at = datetime('now') WHERE id = ?",
                (*changes.values(), task.id),
            )
        return _get(conn, Task, "tasks", task.id)


def transition_task(
    conn: sqlite3.Connection, task_id: int, new_status: str, *, expected: str | None = None
) -> Task:
    """Cambia el estado de una tarea según TASK_TRANSITIONS.

    ``expected``, si se pasa, exige que el estado actual sea ese (evita pisar
    un cambio hecho en paralelo por otra parte de la app).
    """
    if new_status not in TASK_TRANSITIONS:
        raise ValidationError(f"estado desconocido: {new_status!r}")
    with transaction(conn):
        task = _get(conn, Task, "tasks", task_id)
        if expected is not None and task.status != expected:
            raise InvalidTransitionError(
                f"la tarea {task.id} está en '{task.status}', se esperaba '{expected}'"
            )
        if new_status not in TASK_TRANSITIONS[task.status]:
            raise InvalidTransitionError(f"la tarea {task.id} no puede pasar de '{task.status}' a '{new_status}'")
        finished = "datetime('now')" if new_status in TERMINAL_STATUSES else "NULL"
        conn.execute(
            f"UPDATE tasks SET status = ?, updated_at = datetime('now'), finished_at = {finished} WHERE id = ?",
            (new_status, task.id),
        )
        _insert_event(
            conn, task.id, None, "status_change", f"{task.status} → {new_status}", notify=new_status in NOTIFY_STATUSES
        )
        return _get(conn, Task, "tasks", task.id)


def delete_task(conn: sqlite3.Connection, task_id: int) -> bool:
    """Borra una tarea y, en cascada, sus preguntas, ejecuciones y eventos.

    No se puede borrar una tarea en ejecución: primero hay que cancelarla.
    Devuelve False si la tarea no existía.
    """
    with transaction(conn):
        task = _find(conn, Task, "tasks", task_id)
        if task is None:
            return False
        if task.status == "running":
            raise InvalidTransitionError(f"la tarea {task.id} está en ejecución; cancelala antes de borrarla")
        conn.execute("DELETE FROM tasks WHERE id = ?", (task.id,))
        return True


# --- questions ----------------------------------------------------------------


QUESTION_KINDS = ("open", "yes_no")


def ask_question(conn: sqlite3.Connection, task_id: int, question: str, kind: str = "open") -> Question:
    text = _text(question, "question", 4000)
    if kind not in QUESTION_KINDS:
        raise ValidationError(f"tipo de pregunta inválido: {kind!r}")
    with transaction(conn):
        task = _get(conn, Task, "tasks", task_id)
        if task.status in TERMINAL_STATUSES:
            raise InvalidTransitionError(f"la tarea {task.id} ya terminó ('{task.status}')")
        cur = conn.execute("INSERT INTO questions (task_id, question, kind) VALUES (?, ?, ?)", (task.id, text, kind))
        return _get(conn, Question, "questions", cur.lastrowid)


def set_question_message(conn: sqlite3.Connection, question_id: int, tg_chat_id: int, tg_message_id: int) -> Question:
    """Asocia la pregunta con el mensaje de Telegram que la envió (una sola vez)."""
    chat_id = _int(tg_chat_id, "tg_chat_id", 1, 2**53)
    message_id = _int(tg_message_id, "tg_message_id", 1, 2**53)
    with transaction(conn):
        q = _get(conn, Question, "questions", question_id)
        if q.tg_message_id is not None:
            raise InvalidTransitionError(f"la pregunta {q.id} ya tiene mensaje asociado")
        conn.execute(
            "UPDATE questions SET tg_chat_id = ?, tg_message_id = ? WHERE id = ?", (chat_id, message_id, q.id)
        )
        return _get(conn, Question, "questions", q.id)


def answer_question(conn: sqlite3.Connection, question_id: int, answer: str, answered_by: int) -> Question:
    text = _text(answer, "answer", 4000)
    by = _int(answered_by, "answered_by", 1, 2**53)
    with transaction(conn):
        q = _get(conn, Question, "questions", question_id)
        if q.status != "open":
            raise InvalidTransitionError(f"la pregunta {q.id} no está abierta ('{q.status}')")
        conn.execute(
            "UPDATE questions SET answer = ?, answered_by = ?, status = 'answered', answered_at = datetime('now')"
            " WHERE id = ?",
            (text, by, q.id),
        )
        return _get(conn, Question, "questions", q.id)


def expire_question(conn: sqlite3.Connection, question_id: int) -> bool:
    """Marca una pregunta abierta como vencida. Devuelve False si no estaba abierta."""
    with transaction(conn):
        cur = conn.execute(
            "UPDATE questions SET status = 'expired' WHERE id = ? AND status = 'open'", (question_id,)
        )
        return cur.rowcount == 1


def get_question(conn: sqlite3.Connection, question_id: int) -> Question | None:
    return _find(conn, Question, "questions", question_id)


def get_question_by_message(conn: sqlite3.Connection, tg_chat_id: int, tg_message_id: int) -> Question | None:
    """Pregunta enviada en ese mensaje de ese chat (por cualquiera de sus envíos)."""
    row = conn.execute(
        "SELECT * FROM questions WHERE tg_chat_id = ? AND tg_message_id = ?", (tg_chat_id, tg_message_id)
    ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT q.* FROM notification_deliveries d JOIN questions q ON q.id = d.part"
            " WHERE d.part > 0 AND d.chat_id = ? AND d.tg_message_id = ?",
            (tg_chat_id, tg_message_id),
        ).fetchone()
    return Question(**dict(row)) if row else None


def list_questions(conn: sqlite3.Connection, task_id: int, status: str | None = None) -> list[Question]:
    """Preguntas de una tarea en orden, opcionalmente filtradas por estado."""
    if status is None:
        rows = conn.execute("SELECT * FROM questions WHERE task_id = ? ORDER BY id", (task_id,))
    elif status in ("open", "answered", "expired"):
        rows = conn.execute("SELECT * FROM questions WHERE task_id = ? AND status = ? ORDER BY id", (task_id, status))
    else:
        raise ValidationError(f"estado de pregunta inválido: {status!r}")
    return [Question(**dict(row)) for row in rows]


def open_questions_sent_to(conn: sqlite3.Connection, chat_id: int) -> list[Question]:
    """Preguntas abiertas que se le enviaron a este chat, de la más vieja a la más nueva."""
    rows = conn.execute(
        "SELECT DISTINCT q.* FROM questions q JOIN notification_deliveries d ON d.part = q.id"
        " WHERE q.status = 'open' AND d.part > 0 AND d.chat_id = ? AND d.status = 'sent' ORDER BY q.id",
        (chat_id,),
    )
    return [Question(**dict(row)) for row in rows]


def list_open_questions(conn: sqlite3.Connection, task_id: int | None = None) -> list[Question]:
    if task_id is None:
        rows = conn.execute("SELECT * FROM questions WHERE status = 'open' ORDER BY id")
    else:
        rows = conn.execute("SELECT * FROM questions WHERE status = 'open' AND task_id = ? ORDER BY id", (task_id,))
    return [Question(**dict(row)) for row in rows]


# --- runs ---------------------------------------------------------------------


def start_run(conn: sqlite3.Connection, task_id: int) -> Run:
    """Registra el inicio de una ejecución. Solo puede haber una activa por tarea."""
    with transaction(conn):
        task = _get(conn, Task, "tasks", task_id)
        if task.status != "running":
            raise InvalidTransitionError(f"la tarea {task.id} tiene que estar en 'running' (está en '{task.status}')")
        active = conn.execute(
            "SELECT id FROM runs WHERE task_id = ? AND status = 'running'", (task.id,)
        ).fetchone()
        if active:
            raise InvalidTransitionError(f"la tarea {task.id} ya tiene la ejecución {active['id']} activa")
        cur = conn.execute("INSERT INTO runs (task_id) VALUES (?)", (task.id,))
        return _get(conn, Run, "runs", cur.lastrowid)


def set_run_session(conn: sqlite3.Connection, run_id: int, session_id: str) -> Run:
    session = _text(session_id, "session_id", 200)
    with transaction(conn):
        run = _get(conn, Run, "runs", run_id)
        if run.status != "running":
            raise InvalidTransitionError(f"la ejecución {run.id} ya terminó ('{run.status}')")
        conn.execute("UPDATE runs SET session_id = ? WHERE id = ?", (session, run.id))
        return _get(conn, Run, "runs", run.id)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    status: str,
    exit_code: int | None = None,
    summary: str | None = None,
) -> Run:
    if status not in RUN_END_STATUSES:
        raise ValidationError(f"estado final inválido: {status!r}")
    code = None if exit_code is None else _int(exit_code, "exit_code", -(2**31), 2**32)
    text = _text(summary, "summary", 10000, required=False)
    with transaction(conn):
        run = _get(conn, Run, "runs", run_id)
        if run.status != "running":
            raise InvalidTransitionError(f"la ejecución {run.id} ya terminó ('{run.status}')")
        conn.execute(
            "UPDATE runs SET status = ?, exit_code = ?, summary = ?, ended_at = datetime('now') WHERE id = ?",
            (status, code, text, run.id),
        )
        return _get(conn, Run, "runs", run.id)


def get_run(conn: sqlite3.Connection, run_id: int) -> Run | None:
    return _find(conn, Run, "runs", run_id)


def list_runs(conn: sqlite3.Connection, task_id: int) -> list[Run]:
    rows = conn.execute("SELECT * FROM runs WHERE task_id = ? ORDER BY id", (task_id,))
    return [Run(**dict(row)) for row in rows]


def list_active_runs(conn: sqlite3.Connection) -> list[Run]:
    """Ejecuciones marcadas como activas (al arrancar la app, son las que quedaron colgadas)."""
    rows = conn.execute("SELECT * FROM runs WHERE status = 'running' ORDER BY id")
    return [Run(**dict(row)) for row in rows]


# --- task_events --------------------------------------------------------------


def add_event(
    conn: sqlite3.Connection, task_id: int, kind: str, message: str, run_id: int | None = None
) -> TaskEvent:
    """Agrega un evento al feed de la tarea. Estos eventos no se notifican por Telegram.

    Los mensajes largos se truncan (no se rechazan): suelen venir de la salida
    de Claude Code y perder el evento entero sería peor que recortarlo.
    """
    if kind not in EVENT_KINDS:
        raise ValidationError(f"tipo de evento inválido: {kind!r}")
    if not isinstance(message, str) or not message.strip():
        raise ValidationError("message no puede estar vacío")
    with transaction(conn):
        task = _get(conn, Task, "tasks", task_id)
        if run_id is not None:
            run = _get(conn, Run, "runs", run_id)
            if run.task_id != task.id:
                raise ValidationError(f"la ejecución {run.id} no pertenece a la tarea {task.id}")
        event_id = _insert_event(conn, task.id, run_id, kind, message)
        return _get(conn, TaskEvent, "task_events", event_id)


def list_events(
    conn: sqlite3.Connection, task_id: int, *, after_id: int = 0, limit: int = 50
) -> list[TaskEvent]:
    """Eventos de una tarea en orden cronológico, a partir de ``after_id`` (paginación)."""
    rows = conn.execute(
        "SELECT * FROM task_events WHERE task_id = ? AND id > ? ORDER BY id LIMIT ?",
        (task_id, _int(after_id, "after_id", 0, 2**63 - 1), _int(limit, "limit", 1, 1000)),
    )
    return [TaskEvent(**dict(row)) for row in rows]


def pending_notifications(conn: sqlite3.Connection, limit: int = 20) -> list[TaskEvent]:
    """Eventos a notificar todavía no enviados, del más viejo al más nuevo."""
    rows = conn.execute(
        "SELECT * FROM task_events WHERE notify = 1 AND notified = 0 ORDER BY id LIMIT ?",
        (_int(limit, "limit", 1, 1000),),
    )
    return [TaskEvent(**dict(row)) for row in rows]


def mark_notified(conn: sqlite3.Connection, event_ids: Iterable[int]) -> int:
    """Marca eventos como enviados. Devuelve cuántos cambiaron (los ya marcados no cuentan)."""
    ids = sorted({_int(i, "event_id", 1, 2**63 - 1) for i in event_ids})
    if not ids:
        return 0
    changed = 0
    with transaction(conn):
        for start in range(0, len(ids), 500):  # bajo el límite de parámetros de SQLite
            chunk = ids[start : start + 500]
            marks = ", ".join("?" * len(chunk))
            cur = conn.execute(
                f"UPDATE task_events SET notified = 1 WHERE notify = 1 AND notified = 0 AND id IN ({marks})", chunk
            )
            changed += cur.rowcount
    return changed


# --- pedidos de título (botón Nueva tarea) ------------------------------------------------


def add_task_prompt(conn: sqlite3.Connection, chat_id: int, message_id: int, keep: int = 20) -> None:
    """Registra un pedido de título enviado a un chat; se conservan los ``keep`` más nuevos por chat."""
    chat = _int(chat_id, "chat_id", 1, 2**53)
    message = _int(message_id, "message_id", 1, 2**53)
    with transaction(conn):
        conn.execute("INSERT OR IGNORE INTO task_prompts (chat_id, message_id) VALUES (?, ?)", (chat, message))
        conn.execute(
            "DELETE FROM task_prompts WHERE chat_id = ? AND message_id NOT IN"
            " (SELECT message_id FROM task_prompts WHERE chat_id = ? ORDER BY message_id DESC LIMIT ?)",
            (chat, chat, int(keep)),
        )


def take_task_prompt(conn: sqlite3.Connection, chat_id: int, message_id: int) -> bool:
    """True (una sola vez) si ese mensaje es un pedido de título pendiente de ese chat."""
    if type(chat_id) is not int or type(message_id) is not int:
        return False
    with transaction(conn):
        cur = conn.execute("DELETE FROM task_prompts WHERE chat_id = ? AND message_id = ?", (chat_id, message_id))
        return cur.rowcount == 1


def record_delivery(
    conn: sqlite3.Connection,
    event_id: int,
    chat_id: int,
    part: int,
    status: str,
    tg_message_id: int | None = None,
) -> bool:
    """Registra el envío de (una parte de) un evento a un chat. False si ya estaba registrado."""
    if status not in ("sent", "rejected"):
        raise ValidationError(f"estado de envío inválido: {status!r}")
    values = (
        _int(event_id, "event_id", 1, 2**63 - 1),
        _int(chat_id, "chat_id", 1, 2**53),
        _int(part, "part", 0, 2**63 - 1),
        status,
        None if tg_message_id is None else _int(tg_message_id, "tg_message_id", 1, 2**53),
    )
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO notification_deliveries (event_id, chat_id, part, status, tg_message_id)"
            " VALUES (?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
            values,
        )
        return cur.rowcount == 1


def delivered(conn: sqlite3.Connection, event_id: int) -> set[tuple[int, int]]:
    """(chat_id, part) ya registrados para el evento, enviados o rechazados."""
    rows = conn.execute("SELECT chat_id, part FROM notification_deliveries WHERE event_id = ?", (event_id,))
    return {(row["chat_id"], row["part"]) for row in rows}


def _insert_event(
    conn: sqlite3.Connection, task_id: int, run_id: int | None, kind: str, message: str, notify: bool = False
) -> int:
    message = message.strip()
    if len(message) > EVENT_MAX_LEN:
        message = message[: EVENT_MAX_LEN - 1] + "…"
    cur = conn.execute(
        "INSERT INTO task_events (task_id, run_id, kind, message, notify) VALUES (?, ?, ?, ?, ?)",
        (task_id, run_id, kind, message, int(notify)),
    )
    return cur.lastrowid


# --- validación ---------------------------------------------------------------


def normalize_workdir_rel(value: str | None) -> str | None:
    """Valida una subcarpeta relativa a la raíz de trabajo y la normaliza a formato POSIX.

    Rechaza rutas absolutas (en cualquier SO), unidades de Windows y ``..``.
    Es una primera barrera: el sandbox vuelve a verificar con la ruta resuelta.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValidationError("workdir_rel tiene que ser texto")
    if len(value) > 500 or any(ord(c) < 32 for c in value):
        raise ValidationError("workdir_rel es demasiado largo o tiene caracteres de control")
    if PureWindowsPath(value).anchor or PurePosixPath(value).is_absolute():
        raise ValidationError("workdir_rel tiene que ser una ruta relativa")
    parts = [p for p in value.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p == ".." or ":" in p for p in parts):
        raise ValidationError("workdir_rel no puede tener '..' ni ':'")
    return "/".join(parts) or None


def _text(value: Any, field: str, max_len: int, required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str):
        raise ValidationError(f"{field} tiene que ser texto")
    value = value.strip()
    if not value:
        if required:
            raise ValidationError(f"{field} no puede estar vacío")
        return None
    if len(value) > max_len:
        raise ValidationError(f"{field} supera los {max_len} caracteres")
    return value


def _int(value: Any, field: str, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValidationError(f"{field} tiene que ser un entero entre {low} y {high}")
    return value


def _opt_id(value: Any, field: str) -> int | None:
    return None if value is None else _int(value, field, 1, 2**53)


def _find(conn: sqlite3.Connection, cls, table: str, row_id: int):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    return cls(**dict(row)) if row else None


def _get(conn: sqlite3.Connection, cls, table: str, row_id: int):
    found = _find(conn, cls, table, row_id)
    if found is None:
        raise NotFoundError(f"no existe {table} id={row_id}")
    return found
