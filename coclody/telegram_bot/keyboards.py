"""Botones del bot: teclado fijo y botones inline.

- Teclado fijo (``MAIN_KEYBOARD``): sus botones mandan un mensaje de texto
  común con la etiqueta, que ``app.on_text`` reconoce por coincidencia exacta.
- Botones inline: mandan un ``callback_query`` con un ``callback_data`` corto.
  Formato (siempre validado con fullmatch, nunca interpretado de otra forma):

      t:c:<tarea>          cancelar la tarea
      t:r:<tarea>          reintentar la tarea
      q:<pregunta>:s|n     responder sí o no a una pregunta de sí/no

  El ``callback_data`` lo arma el bot, pero vuelve desde Telegram: se trata
  como entrada externa. Un dato que no calza con el formato no hace nada.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from telegram import ForceReply, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup

from coclody.db.repo import Question

BUTTON_NEW = "➕ Nueva tarea"
BUTTON_LIST = "📋 Ver cola"
BUTTON_HELP = "❓ Ayuda"
MAIN_BUTTONS = (BUTTON_NEW, BUTTON_LIST, BUTTON_HELP)

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [[BUTTON_NEW, BUTTON_LIST], [BUTTON_HELP]], resize_keyboard=True, is_persistent=True
)
NEW_TASK_PROMPT = ForceReply(input_field_placeholder="Título de la tarea")

YES, NO = "sí", "no"
_TASK_RE = re.compile(r"t:([cr]):([1-9][0-9]{0,15})")
_ANSWER_RE = re.compile(r"q:([1-9][0-9]{0,15}):([sn])")
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


def parse_callback(data: object) -> TaskAction | AnswerAction | None:
    if not isinstance(data, str):
        return None
    if match := _TASK_RE.fullmatch(data):
        return TaskAction("cancel" if match.group(1) == "c" else "retry", int(match.group(2)))
    if match := _ANSWER_RE.fullmatch(data):
        return AnswerAction(int(match.group(1)), YES if match.group(2) == "s" else NO)
    return None


def task_buttons(actions: list[tuple[int, str]], *, with_id: bool = False) -> InlineKeyboardMarkup | None:
    """Una fila por acción. ``with_id`` agrega el número de tarea (para listas)."""
    rows = []
    for task_id, action in actions:
        text = _ACTION_LABELS[action] + (f" #{task_id}" if with_id else "")
        rows.append([InlineKeyboardButton(text, callback_data=f"t:{_ACTION_CODES[action]}:{task_id}")])
    return InlineKeyboardMarkup(rows) if rows else None


def question_buttons(question: Question) -> InlineKeyboardMarkup | None:
    if question.kind != "yes_no":
        return None
    return InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("Sí", callback_data=f"q:{question.id}:s"),
            InlineKeyboardButton("No", callback_data=f"q:{question.id}:n"),
        ]]
    )
