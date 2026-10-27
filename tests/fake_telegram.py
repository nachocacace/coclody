"""Cliente de la Bot API simulado: intercepta cada llamada HTTP, sin red."""

from __future__ import annotations

import json
import time
from itertools import count

from telegram.request import BaseRequest

TOKEN = "123456789:AAH" + "x" * 32
BOT_USER = {
    "id": 123456789,
    "is_bot": True,
    "first_name": "CoClody",
    "username": "coclody_test_bot",
    "can_join_groups": False,
    "can_read_all_group_messages": False,
    "supports_inline_queries": False,
}


class FakeTelegram(BaseRequest):
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.failures: dict[str, list[tuple[int, str]]] = {}
        self.returned_ids: list[int] = []

    @property
    def read_timeout(self) -> float:
        return 1.0

    async def initialize(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass

    def fail_next(self, method: str, status: int, description: str) -> None:
        self.failures.setdefault(method, []).append((status, description))

    def sent(self) -> list[dict]:
        return [params for name, params in self.calls if name == "sendMessage"]

    def called(self, method: str) -> list[dict]:
        return [params for name, params in self.calls if name == method]

    async def do_request(self, url, method, request_data=None, read_timeout=None, write_timeout=None,
                         connect_timeout=None, pool_timeout=None):
        name = url.rsplit("/", 1)[-1]
        params = dict(request_data.parameters) if request_data else {}
        self.calls.append((name, params))
        if self.failures.get(name):
            status, description = self.failures[name].pop(0)
            return status, json.dumps({"ok": False, "error_code": status, "description": description}).encode()
        if name == "getMe":
            result = BOT_USER
        elif name == "sendMessage":
            chat_id = int(params["chat_id"])
            self.returned_ids.append(next(_message_ids))
            result = {
                "message_id": self.returned_ids[-1],
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "from": BOT_USER,
                "text": params["text"],
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


_update_ids = count(1)
# Global, como en Telegram: un message_id no se repite dentro de un chat.
_message_ids = count(1000)


def message_update(chat_id: int, text: str, *, user_id: int | None = None, chat_type: str = "private",
                   reply_to: int | None = None, message_id: int = 1, date: int | None = None) -> dict:
    message = {
        "message_id": message_id,
        "date": int(time.time()) if date is None else date,
        "chat": {"id": chat_id, "type": chat_type},
        "from": {"id": chat_id if user_id is None else user_id, "is_bot": False, "first_name": "Nacho"},
        "text": text,
    }
    if text.startswith("/"):
        message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
    if reply_to is not None:
        message["reply_to_message"] = {
            "message_id": reply_to,
            "date": 0,
            "chat": {"id": chat_id, "type": chat_type},
            "from": BOT_USER,
            "text": "pregunta",
        }
    return {"update_id": next(_update_ids), "message": message}


# --- payloads con la forma de la Bot API (no la que espera el código) -----------------------------


def bot_message(message_id: int, chat_id: int, text: str, *, date: int | None = None) -> dict:
    """Un mensaje enviado por el bot, como lo devuelve Telegram: con entidades para cada /comando."""
    entities = _command_entities(text)
    msg = {"message_id": message_id, "from": BOT_USER, "chat": {"id": chat_id, "first_name": "Nacho", "type": "private"},
           "date": int(time.time()) if date is None else date, "text": text}
    if entities:
        msg["entities"] = entities
    return msg


def user_text(chat_id: int, text: str, *, reply_to: dict | None = None, external_reply: dict | None = None,
              quote: str | None = None, thread_id: int | None = None, message_id: int = 5000) -> dict:
    """Un mensaje de texto del usuario. ``reply_to`` es el mensaje completo al que responde."""
    msg = {"message_id": message_id, "date": int(time.time()),
           "from": {"id": chat_id, "is_bot": False, "first_name": "Nacho", "language_code": "es"},
           "chat": {"id": chat_id, "first_name": "Nacho", "type": "private"}, "text": text}
    entities = _command_entities(text)
    if entities:
        msg["entities"] = entities
    if reply_to is not None:
        msg["reply_to_message"] = reply_to
    if external_reply is not None:
        msg["external_reply"] = external_reply
    if quote is not None:
        msg["quote"] = {"text": quote, "position": 0}
    if thread_id is not None:
        msg["message_thread_id"] = thread_id
    return {"update_id": next(_update_ids), "message": msg}


def external_reply_to(message: dict) -> dict:
    """Cómo describe Telegram un reply a un mensaje de otro hilo o chat (ExternalReplyInfo)."""
    return {"origin": {"type": "user", "date": message["date"], "sender_user": message["from"]},
            "chat": message["chat"], "message_id": message["message_id"]}


def markup(params: dict) -> dict | None:
    """El reply_markup de una llamada, como dict (PTB lo puede mandar ya serializado)."""
    value = params.get("reply_markup")
    return json.loads(value) if isinstance(value, str) else value


def inline_data(params: dict) -> list[str]:
    """Los callback_data de los botones inline de una llamada, en orden."""
    m = markup(params) or {}
    return [b["callback_data"] for row in m.get("inline_keyboard", []) for b in row]


def bot_message_with_buttons(message_id: int, chat_id: int, params: dict) -> dict:
    """El mensaje del bot tal como vuelve dentro de un callback_query: con su reply_markup."""
    msg = bot_message(message_id, chat_id, params["text"])
    if markup(params) is not None:
        msg["reply_markup"] = markup(params)
    return msg


def callback_update(chat_id: int, data: str | None, *, message: dict | None, user_id: int | None = None,
                    inline_message_id: str | None = None) -> dict:
    """Un botón inline tocado, con la forma de la Bot API (CallbackQuery).

    ``message`` es el mensaje del bot que tenía el botón (su chat es el chat del update);
    ``user_id`` es quien lo tocó. Sin ``message`` y con ``inline_message_id`` es un botón de un
    mensaje enviado en modo inline: no tiene chat. Sin ``data`` es un botón de juego.
    """
    query = {"id": str(next(_update_ids) * 7919), "chat_instance": "-4242",
             "from": {"id": chat_id if user_id is None else user_id, "is_bot": False, "first_name": "Nacho"}}
    if message is not None:
        query["message"] = message
    if inline_message_id is not None:
        query["inline_message_id"] = inline_message_id
    if data is not None:
        query["data"] = data
    else:
        query["game_short_name"] = "juego"
    return {"update_id": next(_update_ids), "callback_query": query}


def _command_entities(text: str) -> list[dict]:
    # Telegram marca cada /comando del texto con una entidad bot_command (offsets en UTF-16).
    return [{"type": "bot_command", "offset": _utf16_len(text[:start]), "length": _utf16_len(word)}
            for start, word in _words(text) if word.startswith("/") and len(word) > 1]


def _words(text: str):
    start = None
    for i, ch in enumerate(text + " "):
        if ch.isspace():
            if start is not None:
                yield start, text[start:i]
                start = None
        elif start is None:
            start = i


def _utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2
