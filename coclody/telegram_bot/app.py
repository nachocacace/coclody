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
- Si el polling muere mientras CoClody sigue abierto (p. ej. InvalidToken
  porque se revocó el token), python-telegram-bot no para la aplicación. Un
  vigilante lo detecta, lo registra como ERROR (también en la consola) y deja
  el proceso en un estado explícito: ``Services.polling_error`` con la causa,
  y el worker y el notificador en pausa (no empieza tareas que nadie podría
  cancelar ni responder). CoClody no se cierra solo y no escribe por Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
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
from coclody.orchestrator import projects as project_ops
from coclody.orchestrator.gitops import Git, GitError
from coclody.orchestrator.keepawake import KeepAwake
from coclody.orchestrator.worker import CANCEL_SHUTDOWN, Worker
from coclody.paths import AppPaths
from coclody.security import ChatGuard
from coclody.telegram_bot.commands import Commands, outgoing, parse_task_id
from coclody.telegram_bot.keyboards import (
    BOT_COMMANDS,
    ORCHESTRATOR_COMMANDS,
    BUTTON_HELP,
    BUTTON_LIST,
    BUTTON_NEW,
    MAIN_KEYBOARD,
    NEW_TASK_PROMPT,
    ApprovalAction,
    AssignAction,
    ContextAction,
    TaskAction,
    TaskDetailAction,
    context_buttons,
    parse_callback,
    task_buttons,
    worker_buttons,
)
from coclody.telegram_bot.notifier import flush_notifications
from coclody.updater.client import Updater, UpdateResult

log = logging.getLogger(__name__)

ALLOWED_UPDATES = ["message", "callback_query"]
WORKER_IDLE_S = 2.0
NOTIFY_INTERVAL_S = 3.0
SHUTDOWN_WAIT_S = 30.0
UPDATE_FIRST_CHECK_S = 60.0
POLL_WATCH_S = 5.0
USAGE_COOLDOWN_S = 900.0  # cuánto esperar antes de reintentar tras toparse con el límite de uso
USAGE_PAUSED_MSG = ("⏸️ Pausé los workers: Claude Code reportó el límite de uso de la suscripción. "
                    "Reintento en unos %d minutos. No hace falta que hagas nada.")
BRAIN_POLL_S = 5.0          # cada cuánto el bucle del cerebro mira si hay eventos
BRAIN_DEBOUNCE_S = 20.0     # espera a juntar eventos antes de invocar al cerebro
BRAIN_BACKOFF_S = (60.0, 120.0, 240.0)  # espera creciente tras respuestas inválidas seguidas
BRAIN_BUDGET_MSG = ("⏸️ Pausé al cerebro: llegó al tope de %s invocaciones. Se reanuda cuando pase la ventana. "
                    "Podés subir el tope en la config.")
POLLING_DEAD = (
    "El polling de Telegram se detuvo por un error (%s). CoClody sigue abierto pero NO recibe mensajes "
    "ni botones; no va a empezar tareas nuevas ni enviar avisos. La tarea en curso, si hay, termina sola. "
    "Revisá el token con 'coclody check' y reiniciá CoClody."
)
_COMMAND_RE = re.compile(r"^/\S+\s*(.*)$", re.DOTALL)


@dataclass
class Services:
    config: Config
    db: DbFactory
    worker: Worker
    guard: ChatGuard
    commands: Commands = field(init=False)
    updater: Updater | None = None
    git: Git | None = None  # solo con el orquestador activado
    app_paths: AppPaths | None = None  # carpetas vacías de hooks y config de git para los workers
    announced_versions: set[str] = field(default_factory=set)
    stop: asyncio.Event | None = None
    background: list[asyncio.Task] = field(default_factory=list)
    stopped: bool = False
    # Causa, si el polling murió mientras CoClody seguía abierto (ver _polling_watch_loop).
    polling_error: str | None = None
    # Momento (monotonic) en que la cola se pausó por el límite de uso de la suscripción, o None.
    usage_paused_at: float | None = None
    usage_notified: bool = False
    brain_budget_notified: bool = False
    keepawake: KeepAwake = field(default_factory=KeepAwake)
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
        if services.config.orchestrator.enabled:
            reply, task_id, choose = c.create_orchestrated_task(chat_id, text)
            if task_id and choose:
                return Reply(reply, worker_buttons(task_id, choose))
            return Reply(reply, task_buttons(c.task_actions(task_id)) if task_id else None)
        reply, task_id = c.create_task(chat_id, text)
        return Reply(reply, task_buttons(c.task_actions(task_id)) if task_id else None)

    def status(update: Update) -> Reply:
        task_id = parse_task_id(_args(update))
        return Reply(c.status(_args(update)), task_buttons(c.task_actions(task_id)) if task_id else None)

    def only_orch(fn):
        return fn if services.config.orchestrator.enabled else (lambda u: ORCHESTRATOR_OFF)

    async def context_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """/contexto: responde el listado y, aparte, cada borrador con botones de aprobar/rechazar."""
        if not services.config.orchestrator.enabled:
            await update.effective_message.reply_text(ORCHESTRATOR_OFF, reply_markup=MAIN_KEYBOARD)
            raise ApplicationHandlerStop
        chat_id = update.effective_chat.id
        text = await asyncio.to_thread(c.context, chat_id, _args(update))
        await update.effective_message.reply_text(outgoing(text), reply_markup=MAIN_KEYBOARD)
        if not _args(update):  # solo al listar: ofrecé cada borrador pendiente con sus botones
            for draft in await asyncio.to_thread(c.pending_context_drafts, chat_id):
                body = f"Borrador de contexto #{draft.id} (propuesto por el cerebro):\n\n{draft.text}"
                await context_bot_message(ctx, chat_id, body, context_buttons(draft.id))
        raise ApplicationHandlerStop

    for names, fn in (
        (("start", "ayuda"), help_),
        (("lista",), list_),
        (("nueva",), lambda u: new(u.effective_chat.id, _args(u))),
        (("estado",), status),
        (("responder",), lambda u: c.answer(u.effective_chat.id, _args(u))),
        (("cancelar",), lambda u: c.cancel(_args(u))),
        (("reintentar",), lambda u: c.retry(_args(u))),
        (("proyectos",), lambda u: list_projects(services)),
        (("proyecto",), lambda u: switch_project(services, _args(u))),
        (("worker",), only_orch(lambda u: c.workers(_args(u)))),
        (("reglas",), only_orch(lambda u: c.rules(_args(u)))),
    ):
        app.add_handler(CommandHandler(list(names), _reply_with(fn), filters=only_new))
    app.add_handler(CommandHandler("contexto", context_cmd, filters=only_new))

    async def cerebro_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not services.config.orchestrator.enabled:
            await update.effective_message.reply_text(ORCHESTRATOR_OFF, reply_markup=MAIN_KEYBOARD)
            raise ApplicationHandlerStop
        plan = await trigger_brain(app, services, "pedido con /cerebro", to=update.effective_chat.id)
        await update.effective_message.reply_text(outgoing(plan), reply_markup=MAIN_KEYBOARD)
        raise ApplicationHandlerStop

    app.add_handler(CommandHandler("cerebro", cerebro_cmd, filters=only_new))

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
        if services.config.orchestrator.enabled and answer_brain_question_reply(services, chat_id, target, message.text):
            return "Le pasé tu respuesta al cerebro."
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
            await update.effective_message.reply_text(outgoing(notice))
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
        sent = await update.effective_message.reply_text(outgoing(reply.text), reply_markup=markup)
        if reply.on_sent is not None:
            await asyncio.to_thread(reply.on_sent, sent)  # puede tocar la base

    return handler


async def context_bot_message(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, text: str, markup) -> None:
    await ctx.bot.send_message(chat_id=chat_id, text=outgoing(text), reply_markup=markup)


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
    elif isinstance(action, AssignAction):
        text = await asyncio.to_thread(c.assign, action.task_id, action.worker)
        remove_buttons = text.startswith("La tarea")  # asignada: los otros botones ya no sirven
    elif isinstance(action, ContextAction):
        text, remove_buttons = await asyncio.to_thread(c.decide_context, action.context_id, action.approve, chat_id)
    elif isinstance(action, ApprovalAction):
        services: Services = context.bot_data["services"]
        from coclody.brain import service as brain_service

        if action.choice == "why":  # ℹ Contexto: muestra el porqué, no resuelve ni saca los botones
            detail = await asyncio.to_thread(brain_service.decision_detail, services.db, action.decision_id)
            await query.answer()
            await context.bot.send_message(chat_id=chat_id, text=outgoing(detail), reply_markup=MAIN_KEYBOARD)
            return
        text, bq_id = await asyncio.to_thread(
            brain_service.approve_decision, services.db, services.config, action.decision_id,
            action.choice == "approve", chat_id)
        remove_buttons = True
        if bq_id:  # la decisión aprobada era un "ask": mando la pregunta del cerebro
            await deliver_brain_question(context.application, services, bq_id)
    elif isinstance(action, TaskDetailAction):
        text = await asyncio.to_thread(_task_detail, context.bot_data["services"], action.task_id)
        await query.answer()
        await context.bot.send_message(chat_id=chat_id, text=outgoing(text), reply_markup=MAIN_KEYBOARD)
        return
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
    await context.bot.send_message(chat_id=chat_id, text=outgoing(text), reply_markup=MAIN_KEYBOARD)


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
            await update.effective_message.reply_text(outgoing("Ocurrió un error interno. Quedó registrado en el log."))
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


def list_projects(services: Services) -> str:
    if not services.config.orchestrator.enabled:
        return ORCHESTRATOR_OFF
    from coclody.db import projects as prepo

    with services.db() as conn:
        rows = prepo.list_projects(conn)
    if not rows:
        return "No hay proyectos. Se agregan desde la computadora: coclody proyecto agregar <nombre> <ruta>"
    lines = [f"{'▶' if p.active else '•'} {p.name}{' (activo)' if p.active else ''}" for p in rows]
    return "Proyectos:\n" + "\n".join(lines) + "\n\nCambiar: /proyecto <nombre>"


def switch_project(services: Services, arg: str) -> str:
    """Cambia el proyecto activo. No cambia nada si hay tareas ejecutándose o si un worktree no sirve."""
    if not services.config.orchestrator.enabled:
        return ORCHESTRATOR_OFF
    name = arg.strip()
    if not name:
        return "Uso: /proyecto <nombre>. Mirá cuáles hay con /proyectos."
    if services.git is None:
        return "git no está disponible; revisá `coclody check` en la computadora."
    try:
        activation = project_ops.activate(services.db, services.git, services.config, name)
    except project_ops.ProjectError as exc:
        lines = [f"No cambié de proyecto: {exc}."]
        lines += [f"- {r.spec.name}: {r.problem}" for r in exc.reports if r.problem]
        return "\n".join(lines)
    except GitError as exc:
        return f"No cambié de proyecto: {exc}"
    created = [r.spec.name for r in activation.reports if r.status == "created"]
    text = f"Proyecto activo: {activation.project.name} (rama base {activation.project.base_branch})."
    if created:
        text += f"\nCreé los worktrees: {', '.join(created)}."
    return text


ORCHESTRATOR_OFF = "El modo orquestador no está activado ([orquestador] enabled = false en la configuración)."


async def trigger_brain(app: Application, services: Services, trigger: str, *, to: int | None = None,
                        event_ids: list[int] | None = None) -> str:
    """Corre el cerebro una vez y entrega sus mensajes (aprobaciones y preguntas). Devuelve el plan.

    En simulacro no toca nada. Con aprobación, manda cada decisión con botones ✓/✗. En autónomo, ya
    ejecutó y manda las preguntas que hayan surgido. ``to`` (chat de /cerebro) o, si es None, la lista blanca.
    """
    from coclody.brain import service as brain_service

    if services.git is None or services.app_paths is None:
        return "El orquestador no está listo (falta git o las carpetas)."
    paths = services.app_paths
    outcome = await asyncio.to_thread(
        brain_service.run_once, services.db, services.config, services.git, paths.brain_dir,
        paths.git_hooks_dir, paths.git_empty_config, trigger, event_ids,
    )
    if outcome is None:
        return "No hay proyecto activo. Elegí uno con /proyecto <nombre>."

    for decision_id, decision in outcome.pending:  # freno de mano: cada decisión en una línea
        await _deliver_approval(app, services, decision_id, f"🧠 {brain_service.describe(decision)}")
    for bq_id in outcome.brain_question_ids:       # preguntas que el cerebro generó (autónomo)
        await deliver_brain_question(app, services, bq_id)
    # Respuesta a /cerebro: en simulacro, el plan (no se mandó nada aparte); si no, una línea (las
    # propuestas ya fueron cada una por su lado; no repito el resumen).
    if outcome.mode == "dry_run":
        return outcome.plan
    if outcome.pending:
        return f"🧠 {len(outcome.pending)} decisión/es; te las mandé para aprobar."
    if outcome.mode == "autonomous" and outcome.decisions:
        return outcome.plan
    return "🧠 Sin propuestas por ahora."


async def _deliver_approval(app: Application, services: Services, decision_id: int, text: str) -> None:
    from coclody.db import repo
    from coclody.telegram_bot.keyboards import approval_buttons

    for chat_id in sorted(services.config.telegram.allowed_chat_ids):
        try:
            sent = await app.bot.send_message(chat_id=chat_id, text=outgoing(text), reply_markup=approval_buttons(decision_id))
            await asyncio.to_thread(_record_approval_delivery, services, decision_id, chat_id, sent.message_id)
        except Exception as exc:
            log.warning("No se pudo enviar la decisión #%s al chat %s: %s", decision_id, chat_id, exc)


def _record_approval_delivery(services: Services, decision_id: int, chat_id: int, message_id: int) -> None:
    from coclody.db import repo

    with services.db() as conn:
        repo.set_approval_delivery(conn, decision_id, chat_id, message_id)


async def deliver_brain_question(app: Application, services: Services, question_id: int) -> None:
    """Manda una pregunta del cerebro a la lista blanca y registra el envío (para responderla con reply)."""
    from coclody.db import repo

    with services.db() as conn:
        q = repo.get_brain_question(conn, question_id)
    if q is None:
        return
    text = (f"🧠❓ El cerebro necesita un dato:\n\n{q.question}\n\n"
            "Respondé a este mensaje (mantené apretado → Responder).")
    for chat_id in sorted(services.config.telegram.allowed_chat_ids):
        try:
            sent = await app.bot.send_message(chat_id=chat_id, text=outgoing(text), reply_markup=MAIN_KEYBOARD)
            await asyncio.to_thread(_record_bq_delivery, services, question_id, chat_id, sent.message_id)
        except Exception as exc:
            log.warning("No se pudo enviar la pregunta del cerebro #%s al chat %s: %s", question_id, chat_id, exc)


def _task_detail(services: Services, task_id: int) -> str:
    """Resumen completo de una tarea terminada (detrás del botón Detalle)."""
    from coclody.db import repo

    with services.db() as conn:
        task = repo.get_task(conn, task_id)
        if task is None:
            return f"No existe la tarea #{task_id}."
        runs = repo.list_runs(conn, task_id)
    summary = runs[-1].summary if runs and runs[-1].summary else "(sin resumen)"
    return f"#{task.id} {task.title}\n{summary}"


def _record_bq_delivery(services: Services, question_id: int, chat_id: int, message_id: int) -> None:
    from coclody.db import repo

    with services.db() as conn:
        repo.record_brain_question_delivery(conn, question_id, chat_id, "sent", message_id)


def answer_brain_question_reply(services: Services, chat_id: int, message_id: int, text: str) -> bool:
    """Si ``message_id`` es el mensaje de una pregunta del cerebro, la responde y despierta al cerebro."""
    from coclody.db import projects as prepo
    from coclody.db import repo

    with services.db() as conn:
        q = repo.brain_question_by_message(conn, chat_id, message_id)
        if q is None or q.status != "open":
            return False
        repo.answer_brain_question(conn, q.id, text, answered_by=chat_id)
        repo.add_brain_event(conn, q.project_id, "user_answer", q.id)
    return True


async def setup_menu(bot, allowed_chat_ids, orchestrator: bool = False) -> int:
    """Registra el menú de comandos en cada chat de la lista blanca. Devuelve en cuántos quedó.

    Es por chat (BotCommandScopeChat), no global: quien no está en la lista no
    ve los comandos. Un error (sin red, o un chat que nunca le escribió al bot)
    se registra y no impide arrancar.
    """
    done = 0
    for chat_id in sorted(allowed_chat_ids):
        try:
            commands = BOT_COMMANDS + (ORCHESTRATOR_COMMANDS if orchestrator else ())
            await bot.set_my_commands(commands, scope=BotCommandScopeChat(chat_id))
            await bot.set_chat_menu_button(chat_id=chat_id, menu_button=MenuButtonCommands())
            done += 1
        except TelegramError as exc:
            log.warning("No se pudo registrar el menú de comandos en el chat %s: %s", chat_id, exc)
    return done


async def _post_init(app: Application) -> None:
    services: Services = app.bot_data["services"]
    await setup_menu(app.bot, services.config.telegram.allowed_chat_ids, services.config.orchestrator.enabled)
    services.stop = asyncio.Event()
    if services.config.orchestrator.enabled:
        services.keepawake.start()  # que la máquina no se duerma con tareas en curso
    if services.config.orchestrator.enabled:
        # Un bucle por worker; el modo de un worker (y su carpeta de trabajo común) no corre.
        workers = [
            asyncio.create_task(_pool_loop(app, services, f"w{i}"), name=f"coclody-worker-w{i}")
            for i in range(1, services.config.orchestrator.workers + 1)
        ]
    else:
        workers = [asyncio.create_task(_worker_loop(services), name="coclody-worker")]
    services.background = [
        *workers,
        asyncio.create_task(_notifier_loop(app, services), name="coclody-notifier"),
        asyncio.create_task(_polling_watch_loop(app, services), name="coclody-polling-watch"),
    ]
    if services.config.orchestrator.enabled:
        services.background.append(asyncio.create_task(_brain_loop(app, services), name="coclody-brain"))
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
    services.keepawake.stop()
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
        if await _paused(services):
            continue
        try:
            task = await asyncio.to_thread(services.worker.run_next)
            _note_usage_limit(services, services.worker.usage_limited)
        except Exception:
            log.exception("Error en el worker")
            task = None
        if task is None:
            await _sleep_or_stop(services.stop, WORKER_IDLE_S)


async def _pool_loop(app: Application, services: Services, name: str) -> None:
    while not services.stop.is_set():
        if await _paused(services, app):
            continue
        try:
            task, usage = await asyncio.to_thread(run_worker_once, services, name)
            _note_usage_limit(services, usage)
        except Exception:
            log.exception("Error en el worker %s", name)
            task = None
        if task is None:
            await _sleep_or_stop(services.stop, WORKER_IDLE_S)


async def _paused(services: Services, app: Application | None = None) -> bool:
    """True (tras dormir un rato) si los workers deben estar en pausa: polling muerto o límite de uso."""
    if services.polling_error is not None:  # en pausa: nadie podría cancelar ni responder
        await _sleep_or_stop(services.stop, WORKER_IDLE_S)
        return True
    paused = services.usage_paused_at
    if paused is not None:
        if time.monotonic() - paused >= USAGE_COOLDOWN_S:
            services.usage_paused_at, services.usage_notified = None, False
            log.info("Reanudo los workers: pasó el enfriamiento por el límite de uso")
            return False
        if app is not None and not services.usage_notified:
            services.usage_notified = True
            await _notify_all(app, services, USAGE_PAUSED_MSG % (USAGE_COOLDOWN_S // 60))
        await _sleep_or_stop(services.stop, WORKER_IDLE_S)
        return True
    return False


def _note_usage_limit(services: Services, hit: bool) -> None:
    if hit and services.usage_paused_at is None:
        services.usage_paused_at = time.monotonic()
        log.warning("Pauso los workers: una tarea se topó con el límite de uso de la suscripción")


async def _notify_all(app: Application, services: Services, text: str) -> None:
    for chat_id in sorted(services.config.telegram.allowed_chat_ids):
        try:
            await app.bot.send_message(chat_id=chat_id, text=outgoing(text), reply_markup=MAIN_KEYBOARD)
        except Exception as exc:
            log.warning("No se pudo avisar al chat %s: %s", chat_id, exc)


def run_worker_once(services: Services, name: str):
    """Corre la próxima tarea del worker ``name``. Devuelve (tarea o None, hubo límite de uso)."""
    from coclody.db import projects as prepo
    from coclody.orchestrator.pool import WorktreeWorker
    from coclody.orchestrator.worktrees import WorkerSpec

    if services.git is None or services.app_paths is None:
        return None, False
    with services.db() as conn:
        project = prepo.active_project(conn)
        row = prepo.get_worker(conn, project.id, name) if project else None
    if row is None or row.state in ("error", "paused"):
        return None, False
    worker = WorktreeWorker(
        services.config, services.db, services.worker.cancels, services.git, project,
        WorkerSpec(row.name, row.branch, row.path_rel), services.app_paths.git_hooks_dir,
        services.app_paths.git_empty_config, runner=services.worker.runner,
    )
    task = worker.run_next()
    if task is not None and not worker.usage_limited:
        _emit_worker_events(services, project.id, name, task)
    return task, worker.usage_limited


def _emit_worker_events(services: Services, project_id: int, name: str, task) -> None:
    """Despierta al cerebro cuando un worker falla, hace una pregunta o queda sin tareas."""
    from coclody.db import repo

    with services.db() as conn:
        if task.status == "failed":
            repo.add_brain_event(conn, project_id, "task_failed", task.id)
        elif task.status == "waiting_input":
            repo.add_brain_event(conn, project_id, "worker_question", task.id)
        if task.status in ("done", "failed") and repo.count_queued_for(conn, project_id, name) == 0:
            repo.add_brain_event(conn, project_id, "worker_idle", None)


async def _brain_loop(app: Application, services: Services) -> None:
    """Invoca al cerebro cuando hay eventos, con debounce, backoff ante respuestas inválidas y tope de uso."""
    from coclody.db import projects as prepo
    from coclody.db import repo

    invalid_streak = 0
    while not services.stop.is_set():
        await _sleep_or_stop(services.stop, BRAIN_POLL_S)
        if services.stop.is_set() or services.polling_error is not None:
            continue
        with services.db() as conn:
            project = prepo.active_project(conn)
            pending = repo.pending_brain_events(conn, project.id) if project else []
        if not project or not pending:
            continue
        await _sleep_or_stop(services.stop, BRAIN_DEBOUNCE_S)  # junta eventos antes de invocar
        if services.stop.is_set():
            continue
        with services.db() as conn:
            pending = repo.pending_brain_events(conn, project.id)
            over = _brain_budget_exceeded(conn, services, project.id)
        if over:
            if not services.brain_budget_notified:
                services.brain_budget_notified = True
                await _notify_all(app, services, BRAIN_BUDGET_MSG % over)
            continue
        services.brain_budget_notified = False
        event_ids = [e["id"] for e in pending]
        trigger = "eventos: " + ", ".join(sorted({e["kind"] for e in pending}))
        outcome = await _run_brain_collecting(app, services, trigger, event_ids)
        if outcome is not None and outcome.parse_status == "invalid":
            invalid_streak += 1
            await _notify_all(app, services, "⚠️ El cerebro devolvió algo inválido; lo descarté. "
                              f"({invalid_streak} seguida/s)")
            await _sleep_or_stop(services.stop, BRAIN_BACKOFF_S[min(invalid_streak - 1, len(BRAIN_BACKOFF_S) - 1)])
            if invalid_streak >= len(BRAIN_BACKOFF_S):
                await _notify_all(app, services, "⏸️ El cerebro falló varias veces seguidas; lo dejo en pausa. "
                                  "Revisá las reglas y volvé a llamarlo con /cerebro.")
                return
        else:
            invalid_streak = 0


def _brain_budget_exceeded(conn, services: Services, project_id: int) -> str | None:
    from coclody.db import repo

    orch = services.config.orchestrator
    if repo.count_brain_runs_since(conn, project_id, "-1 hour") >= orch.brain_calls_per_hour:
        return f"{orch.brain_calls_per_hour}/hora"
    if repo.count_brain_runs_since(conn, project_id, "-1 day") >= orch.brain_calls_per_day:
        return f"{orch.brain_calls_per_day}/día"
    return None


async def _run_brain_collecting(app: Application, services: Services, trigger: str, event_ids: list[int]):
    """Corre el cerebro por eventos y manda el plan a la lista blanca. Devuelve el BrainOutcome (o None)."""
    from coclody.brain import service as brain_service

    paths = services.app_paths
    outcome = await asyncio.to_thread(
        brain_service.run_once, services.db, services.config, services.git, paths.brain_dir,
        paths.git_hooks_dir, paths.git_empty_config, trigger, event_ids)
    if outcome is None:
        return None
    for decision_id, decision in outcome.pending:  # una línea por decisión, el porqué detrás del botón
        await _deliver_approval(app, services, decision_id, f"🧠 {brain_service.describe(decision)}")
    for bq_id in outcome.brain_question_ids:
        await deliver_brain_question(app, services, bq_id)
    # En simulacro te muestro lo que haría (no se mandó nada aparte). En autónomo, el resultado.
    # En aprobación no mando resumen: ya fue cada decisión por su lado.
    if outcome.parse_status == "ok" and (services.config.orchestrator.brain_dry_run or outcome.mode == "autonomous"):
        await _notify_all(app, services, outcome.plan)
    return outcome


async def _notifier_loop(app: Application, services: Services) -> None:
    while not services.stop.is_set():
        if services.polling_error is not None:  # en pausa: los avisos quedan en la base para el próximo arranque
            await _sleep_or_stop(services.stop, NOTIFY_INTERVAL_S)
            continue
        try:
            await flush_notifications(app.bot, services.db, services.config.telegram.allowed_chat_ids)
        except Exception:
            log.exception("Error enviando notificaciones")
        await _sleep_or_stop(services.stop, NOTIFY_INTERVAL_S)


async def _polling_watch_loop(app: Application, services: Services) -> None:
    """Detecta que el polling murió con la aplicación todavía corriendo y lo deja registrado una vez."""
    warned_unwatchable = False
    while not services.stop.is_set():
        failure = polling_failure(app)
        if failure == _UNWATCHABLE:
            if not warned_unwatchable:
                warned_unwatchable = True
                log.warning("No se puede vigilar el polling con esta versión de python-telegram-bot")
        elif failure is not None and not services.stop.is_set():
            services.polling_error = failure
            log.error(POLLING_DEAD, failure)
            return
        await _sleep_or_stop(services.stop, POLL_WATCH_S)


_UNWATCHABLE = "<sin vigilancia>"


def polling_failure(app: Application) -> str | None:
    """Causa si la tarea de polling terminó mientras el updater sigue marcado como corriendo.

    python-telegram-bot no lo expone: se mira su tarea interna. Si esa tarea no
    se encuentra (otra versión de la librería), devuelve ``_UNWATCHABLE`` para
    que se avise en vez de vigilar en silencio algo que no existe.
    """
    updater = app.updater
    if updater is None or not updater.running:
        return None  # todavía no arrancó, o se está parando a propósito
    if not hasattr(updater, "_Updater__polling_task"):
        return _UNWATCHABLE
    task = updater._Updater__polling_task
    if task is None or not task.done():
        return None
    if task.cancelled():
        return "cancelado"
    exc = task.exception()
    return f"{type(exc).__name__}: {exc}" if exc is not None else "terminó sin error"


async def _update_loop(app: Application, services: Services) -> None:
    interval = services.config.updates.check_interval_hours * 3600
    await _sleep_or_stop(services.stop, UPDATE_FIRST_CHECK_S)
    while not services.stop.is_set():
        try:
            result = await asyncio.to_thread(services.updater.check)
            report_update(services, result)
        except Exception:
            log.exception("Error buscando actualizaciones")
        await _sleep_or_stop(services.stop, interval)


def report_update(services: Services, result: UpdateResult) -> None:
    """Registra una sola vez por versión una actualización instalada o disponible.

    Solo en el log (y la consola): CoClody no escribe primero por Telegram
    salvo para avisos de tareas.
    """
    if result.status not in ("installed", "available") or result.version in services.announced_versions:
        return
    services.announced_versions.add(result.version)
    log.warning("Actualización: %s", result.message)


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        pass
