"""Esquema cerrado de las decisiones del cerebro, validado todo-o-nada.

El cerebro consume resúmenes de los workers, que a su vez leyeron archivos del repo: hay una cadena de
inyección posible (archivo hostil → resumen → decisión). Dos defensas:

1. En el prompt, los resúmenes entran como datos delimitados y la instrucción es tratarlos como
   información, nunca como órdenes (ver prompt.py).
2. Acá: la salida del cerebro tiene que ser un JSON con un esquema cerrado. Nada que el esquema no
   pueda expresar es peligroso: no hay forma de agregar comandos a la lista de Bash, de apuntar a una
   ruta fuera del worktree ni de tocar la config de CoClody. Una respuesta que no parsea, trae una
   clave de más, un tipo raro o una acción inválida se descarta **entera** (ninguna decisión se toma)
   y se registra como inválida.

Se aceptan solo ``str`` e ``int`` (sin ``float``, ``bool``, ``None`` anidados salvo donde se indique),
sin caracteres de control, y con topes de largo. ``json.loads`` con ``object_pairs_hook`` rechaza
claves duplicadas.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

MAX_DECISIONS = 5
_MAX_TEXT = 4000
_MAX_REASON = 1000
_MAX_TITLE = 200


class SchemaError(Exception):
    """La respuesta del cerebro no cumple el esquema: se descarta entera."""


@dataclass(frozen=True)
class Decision:
    action: str
    fields: dict  # solo las claves permitidas de la acción, ya validadas
    reason: str

    def payload(self) -> str:
        return json.dumps({"action": self.action, **self.fields, "reason": self.reason}, ensure_ascii=False)


# acción -> (campos obligatorios además de reason, con su validador)
def _text(max_len):
    def check(value, name):
        if not isinstance(value, str):
            raise SchemaError(f"{name} tiene que ser texto")
        if any(ord(ch) < 32 and ch not in "\n\t" for ch in value):
            raise SchemaError(f"{name} tiene caracteres de control")
        value = value.strip()
        if not value:
            raise SchemaError(f"{name} no puede estar vacío")
        if len(value) > max_len:
            raise SchemaError(f"{name} supera {max_len} caracteres")
        return value

    return check


def _worker(valid):
    def check(value, name):
        if value not in valid:
            raise SchemaError(f"{name}={value!r} no es un worker del proyecto")
        return value

    return check


def _kind(value, name):
    if value not in ("open", "yes_no"):
        raise SchemaError(f"{name} tiene que ser 'open' o 'yes_no'")
    return value


def _open_question(valid_ids):
    def check(value, name):
        if type(value) is not int or value not in valid_ids:
            raise SchemaError(f"{name}={value!r} no es una pregunta abierta")
        return value

    return check


def parse_response(text: str | None, *, workers: frozenset[str], open_question_ids: frozenset[int]) -> list[Decision]:
    """Decisiones validadas, o SchemaError si algo no cierra (nada se toma en ese caso)."""
    if not isinstance(text, str) or not text.strip():
        raise SchemaError("respuesta vacía")
    try:
        data = json.loads(text, object_pairs_hook=_no_dup_keys)
    except (json.JSONDecodeError, ValueError) as exc:
        raise SchemaError(f"no es JSON válido: {exc}") from None
    if not isinstance(data, dict) or set(data) != {"decisions"}:
        raise SchemaError("el objeto raíz tiene que ser {\"decisions\": [...]} y nada más")
    raw = data["decisions"]
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_DECISIONS:
        raise SchemaError(f"decisions tiene que ser una lista de 1 a {MAX_DECISIONS} elementos")

    specs = {
        "enqueue": {"worker": _worker(workers), "title": _text(_MAX_TITLE), "description": _text(_MAX_TEXT),
                    "based_on": _text(_MAX_TEXT)},
        "ask": {"question": _text(_MAX_TEXT), "kind": _kind},
        "answer_worker_question": {"question_id": _open_question(open_question_ids), "answer": _text(_MAX_TEXT)},
        "propose_context": {"text": _text(_MAX_TEXT)},
        "wait": {},
    }
    out = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SchemaError(f"la decisión #{i} no es un objeto")
        action = item.get("action")
        if action not in specs:
            raise SchemaError(f"la decisión #{i} tiene una acción inválida: {action!r}")
        allowed = set(specs[action]) | {"action", "reason"}
        extra = set(item) - allowed
        if extra:
            raise SchemaError(f"la decisión #{i} ({action}) tiene claves de más: {', '.join(sorted(extra))}")
        fields = {}
        for key, check in specs[action].items():
            if key not in item:
                raise SchemaError(f"la decisión #{i} ({action}) no tiene '{key}'")
            fields[key] = check(item[key], f"decisión #{i}.{key}")
        reason = _text(_MAX_REASON)(item.get("reason", ""), f"decisión #{i}.reason")
        out.append(Decision(action, fields, reason))
    return out


def _no_dup_keys(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise SchemaError(f"clave duplicada en la respuesta: {key!r}")
        seen[key] = value
    return seen
