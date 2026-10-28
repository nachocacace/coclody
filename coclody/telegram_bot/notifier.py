"""Envío de notificaciones pendientes por Telegram.

Solo se notifican los eventos marcados con ``notify`` (cambios a done, failed
y waiting_input). Destinatario: quien creó la tarea, si sigue en la lista
blanca; si no (o si la tarea no tiene creador), todos los chats de la lista.

Cada envío se registra por destinatario (y por pregunta, en waiting_input).
El evento queda notificado recién cuando todos sus destinatarios tienen su
envío registrado: si un envío falla a mitad de camino, el reintento solo
cubre a los que faltan, sin duplicar a los demás.

Si Telegram rechaza el mensaje de forma permanente (bot bloqueado, chat
inexistente) ese envío se registra como ``rejected`` y se loguea, para no
reintentarlo para siempre. Ante errores transitorios (red, rate limit) se
corta la tanda y se reintenta en la próxima.

Botones: toda pregunta va con Cancelar (y Sí/No si es de sí o no), un fallo con
Reintentar, y el resto con el teclado fijo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError

from coclody.db import DbFactory, repo
from coclody.db.repo import Question, Run, Task, TaskCommit, TaskEvent
from coclody.telegram_bot.commands import STATUS_EMOJI, clip, no_autolink, outgoing, task_title
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from coclody.telegram_bot.keyboards import MAIN_KEYBOARD, question_buttons

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Pending:
    event: TaskEvent
    task: Task
    new_status: str
    last_run: Run | None
    questions: list[Question]
    delivered: set[tuple[int, int]]
    commit: TaskCommit | None = None


def format_done(task: Task, run: Run | None, commit: TaskCommit | None = None) -> str:
    """Una línea: título, estado y commit. El resumen va detrás del botón Detalle."""
    return clip(f"{STATUS_EMOJI['done']} #{task.id} {task_title(task)} · terminada{_commit(commit)}")


def format_failed(task: Task, run: Run | None, commit: TaskCommit | None = None) -> str:
    return clip(f"{STATUS_EMOJI['failed']} #{task.id} {task_title(task)} · falló{_commit(commit)}")


def _commit(commit: TaskCommit | None) -> str:
    """Resumen compacto del commit en la misma línea, con aviso si es grande o toca archivos sensibles."""
    if commit is None:
        return ""
    line = f" · {commit.files} arch +{commit.insertions} −{commit.deletions}"
    if commit.big_change:
        line += " · ⚠️ cambio grande"
    if commit.sensitive_list:
        line += " · ⚠️ sensible: " + ", ".join(commit.sensitive_list)
    return line


def format_question(task: Task, question: Question) -> str:
    buttons = "Tocá Sí o No, o respondé con texto: " if question.kind == "yes_no" else "Para responder: "
    return clip(
        f"{STATUS_EMOJI['waiting_input']} La tarea #{task.id} ({task_title(task)}) necesita un dato:\n\n{no_autolink(question.question)}\n\n"
        f"{buttons}mantené apretado este mensaje → Responder, o mandá /responder {task.id} <respuesta>. "
        "Si contestás desde la notificación, Telegram no lo toma como respuesta a este mensaje."
    )


async def flush_notifications(bot: Bot, db: DbFactory, allowed_chat_ids: Iterable[int], limit: int = 20) -> int:
    """Envía las notificaciones pendientes. Devuelve cuántos eventos se procesaron."""
    allowed = frozenset(allowed_chat_ids)
    if not allowed:
        return 0
    pending = await asyncio.to_thread(_collect, db, limit)
    processed = 0
    for item in pending:
        recipients = [item.task.created_by] if item.task.created_by in allowed else sorted(allowed)
        try:
            await _deliver(bot, db, item, recipients)
        except TelegramError as exc:
            log.warning("Notificación del evento %s pendiente por error transitorio: %s", item.event.id, exc)
            break
        await asyncio.to_thread(_mark, db, item.event.id)
        processed += 1
    return processed


def _detail_markup(task_id: int, retry: bool = False) -> InlineKeyboardMarkup:
    """Botón Detalle (resumen completo) y, en los fallos, Reintentar."""
    row = [InlineKeyboardButton("Detalle", callback_data=f"td:{task_id}")]
    if retry:
        row.append(InlineKeyboardButton("↻ Reintentar", callback_data=f"t:r:{task_id}"))
    return InlineKeyboardMarkup([row])


async def _deliver(bot: Bot, db: DbFactory, item: _Pending, recipients: list[int]) -> None:
    """Envía las partes que falten. Un error transitorio se propaga (corta la tanda)."""
    if item.new_status == "waiting_input":
        parts = [(q.id, format_question(item.task, q), question_buttons(q)) for q in item.questions]
    elif item.new_status == "done":
        parts = [(0, format_done(item.task, item.last_run, item.commit), _detail_markup(item.task.id))]
    elif item.new_status == "failed":
        parts = [(0, format_failed(item.task, item.last_run, item.commit),
                  _detail_markup(item.task.id, retry=item.task.status == "failed"))]
    else:
        return
    for part, text, markup in parts:
        for chat_id in recipients:
            if (chat_id, part) in item.delivered:
                continue
            try:
                sent = await bot.send_message(chat_id=chat_id, text=outgoing(text), reply_markup=markup or MAIN_KEYBOARD)
            except (Forbidden, BadRequest) as exc:
                log.warning("Telegram rechazó la notificación al chat %s: %s", chat_id, exc)
                await asyncio.to_thread(_record, db, item.event.id, chat_id, part, "rejected", None)
                continue
            # Para una pregunta, este registro es también lo que permite responderla con reply.
            await asyncio.to_thread(_record, db, item.event.id, chat_id, part, "sent", sent.message_id)


def _collect(db: DbFactory, limit: int) -> list[_Pending]:
    with db() as conn:
        out = []
        for event in repo.pending_notifications(conn, limit=limit):
            task = repo.get_task(conn, event.task_id)
            runs = repo.list_runs(conn, event.task_id)
            questions = repo.list_open_questions(conn, task_id=event.task_id)
            new_status = event.message.rsplit("→", 1)[-1].strip()
            last = runs[-1] if runs else None
            commit = repo.commit_for_run(conn, last.id) if last else None
            out.append(_Pending(event, task, new_status, last, questions, repo.delivered(conn, event.id), commit))
        return out


def _mark(db: DbFactory, event_id: int) -> None:
    with db() as conn:
        repo.mark_notified(conn, [event_id])


def _record(db: DbFactory, event_id: int, chat_id: int, part: int, status: str, message_id: int | None) -> None:
    with db() as conn:
        repo.record_delivery(conn, event_id, chat_id, part, status, message_id)
