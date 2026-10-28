"""Botones del bot: teclado fijo y botones inline.

- Teclado fijo (``MAIN_KEYBOARD``): sus botones mandan un mensaje de texto
  común con la etiqueta, que ``app.on_text`` reconoce por coincidencia exacta.
  Va en todo mensaje del bot que no lleve otros botones, así se mantiene.
- Menú de comandos (``BOT_COMMANDS``): se registra al arrancar solo para los
  chats de la lista blanca, así está desde el principio, antes de cualquier
  mensaje. Un chat que no está en la lista no ve ningún menú.
- Botones inline: mandan un ``callback_query`` con un ``callback_data`` corto.
  Formato (siempre validado con fullmatch, nunca interpretado de otra forma):

      t:c:<tarea>          cancelar la tarea
      t:r:<tarea>          reintentar la tarea
      w:<tarea>:<worker>   asignar una tarea pendiente a un worker (orquestador)
      cx:a|r:<id>          aprobar o rechazar un borrador de contexto
      q:<pregunta>:s|n     responder sí o no a una pregunta de sí/no

  El ``callback_data`` lo arma el bot, pero vuelve desde Telegram: se trata
  como entrada externa. Un dato que no calza con el formato no hace nada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from telegram import BotCommand, ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from coclody.db.repo import Question

BUTTON_NEW = "➕ Nueva tarea"
BUTTON_LIST = "📋 Ver cola"
BUTTON_HELP = "❓ Ayuda"
MAIN_BUTTONS = (BUTTON_NEW, BUTTON_LIST, BUTTON_HELP)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BUTTON_NEW, BUTTON_LIST], [BUTTON_HELP]], resize_keyboard=True, is_persistent=True
)
NEW_TASK_PROMPT = ForceReply(input_field_placeholder="Título de la tarea")
BOT_COMMANDS = (
    BotCommand("nueva", "Crear una tarea: /nueva <título>"),
    BotCommand("lista", "Ver la cola de tareas"),
    BotCommand("estado", "Detalle de una tarea: /estado <id>"),
    BotCommand("responder", "Responder una pregunta: /responder <id> <texto>"),
    BotCommand("cancelar", "Cancelar una tarea: /cancelar <id>"),
    BotCommand("reintentar", "Reintentar una tarea que falló"),
    BotCommand("ayuda", "Ayuda y botones"),
)
# Solo con el orquestador (modo v2) activado.
ORCHESTRATOR_COMMANDS = (
    BotCommand("proyectos", "Listar los proyectos"),
    BotCommand("proyecto", "Cambiar de proyecto: /proyecto <nombre>"),
    BotCommand("worker", "Ver los workers o reanudar uno: /worker <id> reanudar"),
    BotCommand("reglas", "Ver o editar las reglas del proyecto"),
    BotCommand("contexto", "Ver el contexto y aprobar borradores"),
)

YES, NO = "sí", "no"
_TASK_RE = re.compile(r"t:([cr]):([1-9][0-9]{0,15})")
_ANSWER_RE = re.compile(r"q:([1-9][0-9]{0,15}):([sn])")
_ASSIGN_RE = re.compile(r"w:([1-9][0-9]{0,15}):([a-z0-9][a-z0-9-]{0,15})")
_CONTEXT_RE = re.compile(r"cx:([ar]):([1-9][0-9]{0,15})")
_ACTION_LABELS = {"cancel": "✖ Cancelar", "retry": "↻ Reintentar"}
_ACTION_CODES = {"cancel": "c", "retry": "r"}


@dataclass(frozen=True)
class TaskAction:
    action: str  # "cancel" | "retry"
    task_id: int


@dataclass(frozen=True)
class AnswerAction:
    question_id: int
    answer: str  # YES | NO


@dataclass(frozen=True)
class AssignAction:
    task_id: int
    worker: str


@dataclass(frozen=True)
class ContextAction:
    context_id: int
    approve: bool


def parse_callback(data: object) -> TaskAction | AnswerAction | AssignAction | ContextAction | None:
    if not isinstance(data, str):
        return None
    if match := _TASK_RE.fullmatch(data):
        return TaskAction("cancel" if match.group(1) == "c" else "retry", int(match.group(2)))
    if match := _ANSWER_RE.fullmatch(data):
        return AnswerAction(int(match.group(1)), YES if match.group(2) == "s" else NO)
    if match := _ASSIGN_RE.fullmatch(data):
        return AssignAction(int(match.group(1)), match.group(2))
    if match := _CONTEXT_RE.fullmatch(data):
        return ContextAction(int(match.group(2)), match.group(1) == "a")
    return None


def worker_buttons(task_id: int, workers: list[str]) -> InlineKeyboardMarkup:
    """Un botón por worker para asignar una tarea pendiente (de a tres por fila)."""
    buttons = [InlineKeyboardButton(name, callback_data=f"w:{task_id}:{name}") for name in workers]
    return InlineKeyboardMarkup([buttons[i:i + 3] for i in range(0, len(buttons), 3)])


def task_buttons(actions: list[tuple[int, str]], *, with_id: bool = False) -> InlineKeyboardMarkup | None:
    """Una fila por acción. ``with_id`` agrega el número de tarea (para listas)."""
    rows = []
    for task_id, action in actions:
        text = _ACTION_LABELS[action] + (f" #{task_id}" if with_id else "")
        rows.append([InlineKeyboardButton(text, callback_data=f"t:{_ACTION_CODES[action]}:{task_id}")])
    return InlineKeyboardMarkup(rows) if rows else None


def question_buttons(question: Question) -> InlineKeyboardMarkup:
    """Sí/No si la pregunta es de sí o no, y siempre Cancelar para la tarea que pregunta."""
    rows = []
    if question.kind == "yes_no":
        rows.append([
            InlineKeyboardButton("Sí", callback_data=f"q:{question.id}:s"),
            InlineKeyboardButton("No", callback_data=f"q:{question.id}:n"),
        ])
    rows.append([InlineKeyboardButton(_ACTION_LABELS["cancel"], callback_data=f"t:c:{question.task_id}")])
    return InlineKeyboardMarkup(rows)


def context_buttons(context_id: int) -> InlineKeyboardMarkup:
    """Aprobar / Rechazar un borrador de contexto propuesto por el cerebro."""
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✓ Aprobar", callback_data=f"cx:a:{context_id}"),
        InlineKeyboardButton("✗ Rechazar", callback_data=f"cx:r:{context_id}"),
    ]])
