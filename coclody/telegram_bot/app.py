"""Armado y arranque del bot de Telegram (long polling).

- Todo update pasa primero por ChatGuard (grupo -1). Si no está autorizado,
  se registra en security.log y se corta ahí: ningún otro handler lo ve. Si
  el guard falla por cualquier motivo, también corta: un guard roto deniega.
- Solo se procesan mensajes nuevos escritos por el usuario: los editados, los
  reenviados y los enviados a través de otro bot se ignoran (en el guard y,
  además, en los filtros de cada handler).
- Los botones inline llegan como ``callback_query`` y pasan por el mismo guard:
  lista blanca primero (el chat es el del mensaje con el botón, el usuario es
  quien lo tocó) y, si no está autorizado, se corta antes de tocar la base.
  Un botón sin mensaje (modo inline) o sin datos se ignora. Un callback no
  trae fecha propia (la del mensaje es la de cuando lo envió el bot), así que
  no se filtra por antigüedad: los que estaban acumulados al arrancar los
  descarta ``drop_pending_updates``, y cada acción revalida el estado actual.
- Solo se piden updates de tipo ``message`` y ``callback_query`` a Telegram.
- Al arrancar se descartan los updates que Telegram tenía acumulados
  (``drop_pending_updates``), y además se ignora cualquier mensaje con fecha
  anterior al arranque: un comando de hace horas nunca dispara una tarea.
- Los mensajes se envían como texto plano (sin parse_mode): el contenido de
  una tarea nunca se interpreta como Markdown/HTML.
- En segundo plano corren el worker (una tarea a la vez, en un hilo), el envío
  de notificaciones y, si están habilitadas, las actualizaciones. Al parar, se
  cancela la tarea en curso y se espera a que el worker la cierre. Esa limpieza
  corre también si el polling termina con error (``run_polling``).
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from telegram import (
    BotCommandScopeChat,
    ForceReply,
    InlineKeyboardMarkup,
    MenuButtonCommands,
    Message,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ApplicationHandlerStop,
    CallbackQueryHandler,
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
from coclody.telegram_bot.commands import Commands, clip, parse_task_id
from coclody.telegram_bot.keyboards import (
    BOT_COMMANDS,
    BUTTON_HELP,
    BUTTON_LIST,
    BUTTON_NEW,
    MAIN_KEYBOARD,
    NEW_TASK_PROMPT,
    TaskAction,
    parse_callback,
    task_buttons,
)
from coclody.telegram_bot.notifier import flush_notifications
from coclody.updater.client import Updater, UpdateResult

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]
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
    stopped: bool = False
    # Telegram fecha los mensajes con resolución de segundos.
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC).replace(microsecond=0))

    def __post_init__(self) -> None:
        self.commands = Commands(self.db, self.worker.cancels)


@dataclass(frozen=True)
class Reply:
    """Respuesta de un handler: texto, botones opcionales y qué hacer con el mensaje enviado."""

    text: str
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup | ForceReply | None = None
    on_sent: Callable[[Message], None] | None = None


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
    only_new = filters.UpdateType.MESSAGE & ~filters.FORWARDED & ~filters.VIA_BOT

    def help_(update: Update) -> Reply:
        return Reply(c.help(), MAIN_KEYBOARD)

    def list_(update: Update) -> Reply:
        return Reply(c.list_tasks(), task_buttons(c.list_actions(), with_id=True))

    def new(chat_id: int, text: str) -> Reply:
        reply, task_id = c.create_task(chat_id, text)
        return Reply(reply, task_buttons(c.task_actions(task_id)) if task_id else None)

    def status(update: Update) -> Reply:
        task_id = parse_task_id(_args(update))
        return Reply(c.status(_args(update)), task_buttons(c.task_actions(task_id)) if task_id else None)

    for names, fn in (
        (("start", "ayuda"), help_),
        (("lista",), list_),
        (("nueva",), lambda u: new(u.effective_chat.id, _args(u))),
        (("estado",), status),
        (("responder",), lambda u: c.answer(u.effective_chat.id, _args(u))),
        (("cancelar",), lambda u: c.cancel(_args(u))),
        (("reintentar",), lambda u: c.retry(_args(u))),
    ):
        app.add_handler(CommandHandler(list(names), _reply_with(fn), filters=only_new))

    def on_text(update: Update) -> str | Reply:
        message, chat_id = update.effective_message, update.effective_chat.id
        # Los botones del teclado fijo llegan como texto con su etiqueta exacta. Van primero: si el
        # cliente los mandara como reply a una pregunta, no deben quedar registrados como respuesta.
        if message.text == BUTTON_NEW:
            return Reply(
                "Escribí el título de la tarea como respuesta a este mensaje. "
                "Las líneas siguientes al título son la descripción.",
                NEW_TASK_PROMPT,
                on_sent=lambda sent: c.remember_task_prompt(chat_id, sent.message_id),
            )
        if message.text == BUTTON_LIST:
            return list_(update)
        if message.text == BUTTON_HELP:
            return help_(update)
        target = replied_message_id(message, chat_id)
        if target is None:
            log.info("Texto sin reply a una pregunta: %s", _reply_shape(message))
            return c.unmatched_text(chat_id)
        if c.take_task_prompt(chat_id, target):
            return new(chat_id, message.text)
        text = c.answer_reply(chat_id, target, message.text)
        if text is not None:
            return text
        return (
            "Ese mensaje no es una pregunta pendiente ni un pedido de título vigente. "
            f"Usá /responder <id> <texto>, o tocá «{BUTTON_NEW}» de nuevo."
        )

    # Todo texto que no sea comando pasa por on_text: la decisión de si es una respuesta se toma
    # en código (replied_message_id), no en un filtro que dependa de la forma exacta del update.
    app.add_handler(MessageHandler(only_new & filters.TEXT & ~filters.COMMAND, _reply_with(on_text)))
    app.add_handler(MessageHandler(only_new, _reply_with(lambda u: "No entendí. Mandá /ayuda para ver los comandos.")))
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_error_handler(_on_error)
    return app


def run_polling(app: Application) -> None:
    """Corre el bot hasta que se lo pare, y siempre deja todo cerrado al salir.

    Si el polling termina con error (p. ej. el token se revocó: InvalidToken),
    ``Application.run_polling`` vuelve a lanzar ese error al parar el updater,
    dentro de su propio ``finally``, y se saltea stop, post_stop y shutdown.
    Por eso el loop es nuestro (``close_loop=False``) y la limpieza corre acá
    en cualquier caso: cancelar la tarea en curso, esperar al worker, enviar
    lo pendiente y cerrar la aplicación. En el camino normal ya corrió en
    post_stop y acá no hace nada. El error original se sigue propagando.
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        app.run_polling(allowed_updates=ALLOWED_UPDATES, drop_pending_updates=True, close_loop=False)
    finally:
        try:
            loop.run_until_complete(_close(app))
        finally:
            asyncio.set_event_loop(None)
            loop.close()


# --- handlers -------------------------------------------------------------------


FORWARDED_NOTICE = "Ignoré un mensaje reenviado: los comandos tienen que escribirse directamente."


async def _guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Deja pasar solo mensajes nuevos de un chat autorizado. Ante cualquier error, deniega."""
    try:
        verdict, notice = _verdict(update, context.bot_data["services"])
    except Exception:
        log.exception("El guard falló; el update se descarta")
        raise ApplicationHandlerStop from None
    if verdict == "ok":
        return
    if notice:
        try:
            await update.effective_message.reply_text(notice)
        except Exception:
            log.exception("No se pudo avisar que se ignoró un mensaje")
    raise ApplicationHandlerStop


def _verdict(update: Update, services: Services) -> tuple[str, str | None]:
    chat, user = update.effective_chat, update.effective_user
    kind = next((name for name in Update.ALL_TYPES if getattr(update, name, None) is not None), "other")
    if not services.guard.is_authorized(
        chat.id if chat else None, user.id if user else None, chat.type if chat else None, kind
    ):
        return "deny", None
    query = update.callback_query
    if query is not None:
        # Sin mensaje (botón de un mensaje inline) no hay chat que validar; sin datos (juegos) no hay acción.
        if query.message is None or not isinstance(query.data, str):
            log.info("Ignorado un botón sin mensaje o sin datos")
            return "ignore", None
        return "ok", None
    message = update.message  # solo mensajes nuevos; edited_message y el resto no
    if message is None:
        log.info("Ignorado un update de tipo %s", kind)
        return "ignore", None
    if message.date < services.started_at:
        log.info("Ignorado un mensaje anterior al arranque (%s)", message.date.isoformat())
        return "ignore", None
    if message.forward_origin is not None or message.is_automatic_forward or message.via_bot is not None:
        log.info("Ignorado un mensaje reenviado o enviado vía otro bot")
        return "ignore", FORWARDED_NOTICE
    return "ok", None


def _reply_with(fn):
    async def handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        reply = await asyncio.to_thread(fn, update)
        if isinstance(reply, str):
            reply = Reply(reply)
        if reply is None or not reply.text:
            return
        # Sin otros botones, va el teclado fijo: así sigue visible en cada respuesta.
        markup = reply.markup if reply.markup is not None else MAIN_KEYBOARD
        sent = await update.effective_message.reply_text(clip(reply.text), reply_markup=markup)
        if reply.on_sent is not None:
            reply.on_sent(sent)

    return handler


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Un botón inline. El guard ya validó el chat y el usuario; el dato se valida acá."""
    c: Commands = context.bot_data["services"].commands
    query, chat_id = update.callback_query, update.effective_chat.id
    action = parse_callback(query.data)
    if action is None:
        log.info("Ignorado un botón con datos no reconocidos")
        await query.answer("Ese botón no es válido.")
        return
    if isinstance(action, TaskAction):
        fn = c.cancel if action.action == "cancel" else c.retry
        text, remove_buttons = await asyncio.to_thread(fn, str(action.task_id)), False
    else:
        text, remove_buttons = await asyncio.to_thread(
            c.answer_button, chat_id, query.message.message_id, action.question_id, action.answer
        )
        text = f"Respuesta: {action.answer}. {text}" if text.startswith("Respuesta registrada") else text
    await query.answer()
    if remove_buttons:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except TelegramError as exc:
            log.warning("No se pudieron sacar los botones del mensaje: %s", exc)
    await context.bot.send_message(chat_id=chat_id, text=clip(text), reply_markup=MAIN_KEYBOARD)


def replied_message_id(message, chat_id: int) -> int | None:
    """message_id del mensaje al que responde, si es de este mismo chat.

    Telegram usa ``reply_to_message`` para replies en el mismo chat e hilo, y
    ``external_reply`` cuando el mensaje citado está en otro hilo (p. ej. con
    los hilos de chats privados activados) o en otro chat. Solo se acepta el
    mismo chat: un reply a un mensaje de otro chat nunca es una respuesta.
    """
    if message.reply_to_message is not None:
        return message.reply_to_message.message_id
    external = message.external_reply
    if external is not None and external.chat is not None and external.chat.id == chat_id and external.message_id:
        return external.message_id
    return None


def _reply_shape(message) -> str:
    # Solo la forma (qué campos de reply trae), nunca el texto: sirve para diagnosticar clientes raros.
    return (
        f"reply_to={message.reply_to_message is not None} external_reply={message.external_reply is not None} "
        f"quote={message.quote is not None} thread={message.message_thread_id}"
    )


def _args(update: Update) -> str:
    match = _COMMAND_RE.match(update.effective_message.text or "")
    return match.group(1) if match else ""


async def _on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Registra el error, avisa solo a un chat autorizado y corta cualquier handler que quedara."""
    log.error("Error procesando un update", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message is not None and _permitted(update, context):
        try:
            await update.effective_message.reply_text("Ocurrió un error interno. Quedó registrado en el log.")
        except Exception:
            log.exception("No se pudo avisar del error")
    raise ApplicationHandlerStop


def _permitted(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        chat, user = update.effective_chat, update.effective_user
        guard: ChatGuard = context.bot_data["services"].guard
        return guard.permits(chat.id if chat else None, user.id if user else None, chat.type if chat else None)
    except Exception:
        return False


# --- bucles en segundo plano -------------------------------------------------------


async def setup_menu(bot, allowed_chat_ids) -> int:
    """Registra el menú de comandos en cada chat de la lista blanca. Devuelve en cuántos quedó.

    Es por chat (BotCommandScopeChat), no global: quien no está en la lista no
    ve los comandos. Un error (sin red, o un chat que nunca le escribió al bot)
    se registra y no impide arrancar.
    """
    done = 0
    for chat_id in sorted(allowed_chat_ids):
        try:
            await bot.set_my_commands(BOT_COMMANDS, scope=BotCommandScopeChat(chat_id))
            await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonCommands())
            done += 1
        except TelegramError as exc:
            log.warning("No se pudo registrar el menú de comandos en el chat %s: %s", chat_id, exc)
    return done


async def _post_init(app: Application) -> None:
    services: Services = app.bot_data["services"]
    await setup_menu(app.bot, services.config.telegram.allowed_chat_ids)
    services.stop = asyncio.Event()
    services.background = [
        asyncio.create_task(_worker_loop(services), name="coclody-worker"),
        asyncio.create_task(_notifier_loop(app, services), name="coclody-notifier"),
    ]
    if services.updater is not None:
        services.background.append(asyncio.create_task(_update_loop(app, services), name="coclody-updater"))


async def _post_stop(app: Application) -> None:
    await _stop_services(app)


async def _close(app: Application) -> None:
    """Limpieza final de run_polling; en el camino normal todo ya está cerrado y no hace nada."""
    await _stop_services(app)
    try:
        if app.running:
            await app.stop()
        await app.shutdown()
    except Exception:
        log.exception("No se pudo cerrar la aplicación de Telegram")


async def _stop_services(app: Application) -> None:
    """Para los bucles de fondo una sola vez, aunque se llame desde post_stop y desde _close."""
    services: Services = app.bot_data["services"]
    if services.stop is None or services.stopped:
        return
    services.stopped = True
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
            await bot.send_message(chat_id=chat_id, text=prefix + result.message, reply_markup=MAIN_KEYBOARD)
        except Exception as exc:
            log.warning("No se pudo avisar la actualización al chat %s: %s", chat_id, exc)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
