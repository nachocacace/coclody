"""Cliente de la Bot API simulado: intercepta cada llamada HTTP, sin red."""

from __future__ import annotations

import json
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
        self._ids = count(1000)

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
            result = {
                "message_id": next(self._ids),
                "date": 0,
                "chat": {"id": chat_id, "type": "private"},
                "from": BOT_USER,
                "text": params["text"],
            }
        else:
            result = True
        return 200, json.dumps({"ok": True, "result": result}).encode()


_update_ids = count(1)


def message_update(chat_id: int, text: str, *, user_id: int | None = None, chat_type: str = "private",
                   reply_to: int | None = None, message_id: int = 1) -> dict:
    message = {
        "message_id": message_id,
        "date": 0,
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
