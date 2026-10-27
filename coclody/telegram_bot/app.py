"""Armado y arranque del bot de Telegram (long polling).

- Todo update pasa primero por ChatGuard (grupo -1). Si no está autorizado,
  se registra en security.log y se corta ahí: ningún otro handler lo ve.
- Solo se piden updates de tipo ``message`` a Telegram.
- Al arrancar se descartan los updates que Telegram tenía acumulados
  (``drop_pending_updates``), y además se ignora cualquier mensaje con fecha
  anterior al arranque: un comando de hace horas nunca dispara una tarea.
- Los mensajes se envían como texto plano (sin parse_mode): el contenido de
  una tarea nunca se interpreta como Markdown/HTML.
- En segundo plano corren el worker (una tarea a la vez, en un hilo), el envío
  de notificaciones y, si están habilitadas, las actualizaciones. Al parar, se
  cancela la tarea en curso y se espera a que el worker la cierre.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

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
from coclody.updater.client import Updater, UpdateResult

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message"]
WORKER_IDLE_S = 2.0
NOTIFY_INTERVAL_S = 3.0
SHUTDOWN_WAIT_S = 30.0
UPDATE_FIRST_CHECK_S = 60.0
_COMMAND_RE = re.compile(r"^/\S+\s*(.*)$", re.DOTALL)


@dataclass
class Services:
    config: Config
    db: DbFactory
    worker: Worker
    guard: ChatGuard
    commands: Commands = field(init=False)
    updater: Updater | None = None
    announced_versions: set[str] = field(default_factory=set)
    stop: asyncio.Event | None = None
    background: list[asyncio.Task] = field(default_factory=list)
    # Telegram fecha los mensajes con resolución de segundos.
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

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
        (("reintentar",), lambda u: c.retry(_args(u))),
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
    app.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=True)


# --- handlers -------------------------------------------------------------------


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    services: Services = context.bot_data["services"]
    chat, user = update.effective_chat, update.effective_user
    kind = next((name for name in Update.ALL_TYPES if getattr(update, name, None) is not None), "other")
    if not services.guard.is_authorized(
        chat.id if chat else None, user.id if user else None, chat.type if chat else None, kind
    ):
        raise ApplicationHandlerStop
    message = update.effective_message
    if message is not None and message.date < services.started_at:
        log.info("Ignorado un mensaje anterior al arranque (%s)", message.date.isoformat())
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
    if services.updater is not None:
        services.background.append(asyncio.create_task(_update_loop(app, services), name="coclody-updater"))


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


async def _update_loop(app: Application, services: Services) -> None:
    interval = services.config.updates.check_interval_hours * 3600
    await _sleep_or_stop(services.stop, UPDATE_FIRST_CHECK_S)
    while not services.stop.is_set():
        try:
            result = await asyncio.to_thread(services.updater.check)
            await announce_update(app.bot, services, result)
        except Exception:
            log.exception("Error buscando actualizaciones")
        await _sleep_or_stop(services.stop, interval)


async def announce_update(bot, services: Services, result: UpdateResult) -> None:
    """Avisa por Telegram una sola vez por versión instalada o disponible."""
    if result.status not in ("installed", "available") or result.version in services.announced_versions:
        return
    services.announced_versions.add(result.version)
    prefix = "⬆️ " if result.status == "installed" else "ℹ️ "
    for chat_id in sorted(services.config.telegram.allowed_chat_ids):
        try:
            await bot.send_message(chat_id=chat_id, text=prefix + result.message)
        except Exception as exc:
            log.warning("No se pudo avisar la actualización al chat %s: %s", chat_id, exc)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
