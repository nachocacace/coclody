"""Armado y arranque del bot de Telegram (long polling).

- Todo update pasa primero por ChatGuard (grupo -1). Si no está autorizado,
  se registra en security.log y se corta ahí: ningún otro handler lo ve.
- Solo se piden updates de tipo ``message`` a Telegram.
- Los mensajes se envían como texto plano (sin parse_mode): el contenido de
  una tarea nunca se interpreta como Markdown/HTML.
- En segundo plano corren dos bucles: el worker (una tarea a la vez, en un
  hilo) y el envío de notificaciones. Al parar, se cancela la tarea en curso
  y se espera a que el worker la cierre.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    TypeHandler,
    filters,
)
from telegram.request import BaseRequest

from coclody.config import Config
from coclody.db import DbFactory
from coclody.orchestrator.worker import CANCEL_SHUTDOWN, Worker
from coclody.security import ChatGuard
from coclody.telegram_bot.commands import Commands, clip
from coclody.telegram_bot.notifier import flush_notifications

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message"]
WORKER_IDLE_S = 2.0
NOTIFY_INTERVAL_S = 3.0
SHUTDOWN_WAIT_S = 30.0
_COMMAND_RE = re.compile(r"^/\S+\s*(.*)$", re.DOTALL)


@dataclass
class Services:
    config: Config
    db: DbFactory
    worker: Worker
    guard: ChatGuard
    commands: Commands = field(init=False)
    stop: asyncio.Event | None = None
    background: list[asyncio.Task] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.commands = Commands(self.db, self.worker.cancels)


def build_application(
    token: str,
    services: Services,
    *,
    request: BaseRequest | None = None,
    get_updates_request: BaseRequest | None = None,
) -> Application:
    builder = ApplicationBuilder().token(token).post_init(_post_init).post_stop(_post_stop)
    if request is not None:
        builder = builder.request(request)
    if get_updates_request is not None:
        builder = builder.get_updates_request(get_updates_request)
    app = builder.build()
    app.bot_data["services"] = services

    app.add_handler(TypeHandler(Update, _guard), group=-1)
    c = services.commands
    for names, fn in (
        (("start", "ayuda"), lambda u: c.help()),
        (("lista",), lambda u: c.list_tasks()),
        (("nueva",), lambda u: c.new_task(u.effective_chat.id, _args(u))),
        (("estado",), lambda u: c.status(_args(u))),
        (("responder",), lambda u: c.answer(u.effective_chat.id, _args(u))),
        (("cancelar",), lambda u: c.cancel(_args(u))),
    ):
        app.add_handler(CommandHandler(list(names), _reply_with(fn)))

    def on_reply(update: Update) -> str:
        message = update.effective_message
        text = c.answer_reply(update.effective_chat.id, message.reply_to_message.message_id, message.text)
        return text if text is not None else "Ese mensaje no es una pregunta pendiente. Usá /responder <id> <texto>."

    app.add_handler(MessageHandler(filters.REPLY & filters.TEXT & ~filters.COMMAND, _reply_with(on_reply)))
    app.add_handler(MessageHandler(filters.ALL, _reply_with(lambda u: "No entendí. Mandá /ayuda para ver los comandos.")))
    app.add_error_handler(_on_error)
    return app


def run_polling(app: Application) -> None:
    app.run_polling(allowed_updates=ALLOWED_UPDATES)


# --- handlers -------------------------------------------------------------------


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services: Services = context.bot_data["services"]
    chat, user = update.effective_chat, update.effective_user
    kind = next((name for name in Update.ALL_TYPES if getattr(update, name, None) is not None), "other")
    if not services.guard.is_authorized(
        chat.id if chat else None, user.id if user else None, chat.type if chat else None, kind
    ):
        raise ApplicationHandlerStop


def _reply_with(fn):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text = await asyncio.to_thread(fn, update)
        if text:
            await update.effective_message.reply_text(clip(text))

    return handler


def _args(update: Update) -> str:
    match = _COMMAND_RE.match(update.effective_message.text or "")
    return match.group(1) if match else ""


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("Error procesando un update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None:
        try:
            await update.effective_message.reply_text("Ocurrió un error interno. Quedó registrado en el log.")
        except Exception:
            log.exception("No se pudo avisar del error")


# --- bucles en segundo plano -------------------------------------------------------


async def _post_init(app: Application) -> None:
    services: Services = app.bot_data["services"]
    services.stop = asyncio.Event()
    services.background = [
        asyncio.create_task(_worker_loop(services), name="coclody-worker"),
        asyncio.create_task(_notifier_loop(app, services), name="coclody-notifier"),
    ]


async def _post_stop(app: Application) -> None:
    services: Services = app.bot_data["services"]
    if services.stop is None:
        return
    services.stop.set()
    services.worker.cancels.cancel_all(CANCEL_SHUTDOWN)
    done, pending = await asyncio.wait(services.background, timeout=SHUTDOWN_WAIT_S)
    for task in pending:
        task.cancel()
    try:  # último intento de avisar lo que haya quedado pendiente
        await flush_notifications(app.bot, services.db, services.config.telegram.allowed_chat_ids)
    except Exception:
        log.exception("No se pudieron enviar las últimas notificaciones")


async def _worker_loop(services: Services) -> None:
    while not services.stop.is_set():
        try:
            task = await asyncio.to_thread(services.worker.run_next)
        except Exception:
            log.exception("Error en el worker")
            task = None
        if task is None:
            await _sleep_or_stop(services.stop, WORKER_IDLE_S)


async def _notifier_loop(app: Application, services: Services) -> None:
    while not services.stop.is_set():
        try:
            await flush_notifications(app.bot, services.db, services.config.telegram.allowed_chat_ids)
        except Exception:
            log.exception("Error enviando notificaciones")
        await _sleep_or_stop(services.stop, NOTIFY_INTERVAL_S)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
