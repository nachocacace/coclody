"""Envío de notificaciones pendientes por Telegram.

Solo se notifican los eventos marcados con ``notify`` (cambios a done, failed
y waiting_input). Destinatario: quien creó la tarea, si sigue en la lista
blanca; si no (o si la tarea no tiene creador), todos los chats de la lista.

Si Telegram rechaza el mensaje de forma permanente (bot bloqueado, chat
inexistente) el evento se marca igual como enviado y se loguea, para no
reintentarlo para siempre. Ante errores transitorios (red, rate limit) se
corta la tanda y se reintenta en la próxima.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass

from telegram import Bot
from telegram.error import BadRequest, Forbidden, TelegramError

from coclody.db import DbFactory, repo
from coclody.db.repo import Question, Run, Task, TaskEvent
from coclody.telegram_bot.commands import clip

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class _Pending:
    event: TaskEvent
    task: Task
    new_status: str
    last_run: Run | None
    questions: list[Question]


def format_done(task: Task, run: Run | None) -> str:
    return clip(f"✅ Tarea #{task.id} terminada: {task.title}\n\n{_summary(run)}")


def format_failed(task: Task, run: Run | None) -> str:
    return clip(f"❌ Tarea #{task.id} falló: {task.title}\n\n{_summary(run)}\n\nDetalle: /estado {task.id}")


def format_question(task: Task, question: Question) -> str:
    return clip(
        f"❓ La tarea #{task.id} ({task.title}) necesita un dato:\n\n{question.question}\n\n"
        f"Respondé a este mensaje, o mandá /responder {task.id} <respuesta>."
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


async def _deliver(bot: Bot, db: DbFactory, item: _Pending, recipients: list[int]) -> None:
    if item.new_status == "waiting_input":
        messages = [(q, format_question(item.task, q)) for q in item.questions]
    elif item.new_status == "done":
        messages = [(None, format_done(item.task, item.last_run))]
    elif item.new_status == "failed":
        messages = [(None, format_failed(item.task, item.last_run))]
    else:
        return
    for question, text in messages:
        for chat_id in recipients:
            try:
                sent = await bot.send_message(chat_id=chat_id, text=text)
            except (Forbidden, BadRequest) as exc:
                log.warning("Telegram rechazó la notificación al chat %s: %s", chat_id, exc)
                continue
            if question is not None:
                await asyncio.to_thread(_link_question, db, question.id, chat_id, sent.message_id)
                question = None  # se asocia solo el primer mensaje enviado


def _collect(db: DbFactory, limit: int) -> list[_Pending]:
    with db() as conn:
        out = []
        for event in repo.pending_notifications(conn, limit=limit):
            task = repo.get_task(conn, event.task_id)
            runs = repo.list_runs(conn, event.task_id)
            questions = [q for q in repo.list_open_questions(conn, task_id=event.task_id) if q.tg_message_id is None]
            new_status = event.message.rsplit("→", 1)[-1].strip()
            out.append(_Pending(event, task, new_status, runs[-1] if runs else None, questions))
        return out


def _mark(db: DbFactory, event_id: int) -> None:
    with db() as conn:
        repo.mark_notified(conn, [event_id])


def _link_question(db: DbFactory, question_id: int, chat_id: int, message_id: int) -> None:
    with db() as conn:
        try:
            repo.set_question_message(conn, question_id, chat_id, message_id)
        except repo.InvalidTransitionError:
            pass  # ya estaba asociada


def _summary(run: Run | None) -> str:
    if run is None or not run.summary:
        return "(sin resumen)"
    return clip(run.summary, 3500)
