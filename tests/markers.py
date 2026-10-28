"""Ayuda para los Claude falsos: emitir el marcador de pregunta que el prompt les indicó.

El worker arma el marcador con un nonce aleatorio por ejecución y lo pone en el prompt. Un Claude
real leería ese marcador del prompt; los fakes hacen lo mismo con esta función, así el nonce real
viaja de punta a punta en vez de hardcodear un marcador que no coincidiría.
"""

from __future__ import annotations

import re

from coclody.orchestrator.worker import QUESTION_BASE, YES_NO_BASE

# El marcador abierto es COCLODY_PREGUNTA_<hex>: y el de sí/no COCLODY_PREGUNTA_SINO_<hex>:. Como "SINO"
# no es hexadecimal, el patrón del abierto nunca matchea al de sí/no.
_OPEN_RE = re.compile(rf"{QUESTION_BASE}_[0-9a-f]+:")
_YES_NO_RE = re.compile(rf"{YES_NO_BASE}_[0-9a-f]+:")


def marker_in(prompt: str, kind: str = "open") -> str:
    """El marcador exacto (con nonce) que el prompt pide para una pregunta abierta o de sí/no."""
    match = (_YES_NO_RE if kind == "yes_no" else _OPEN_RE).search(prompt)
    if match is None:
        raise AssertionError(f"el prompt no trae el marcador {kind}")
    return match.group(0)


def ask(prompt: str, question: str, kind: str = "open") -> str:
    """Texto de resultado de Claude que termina con el marcador correcto (lo que dispara una pregunta)."""
    return f"Necesito un dato.\n{marker_in(prompt, kind)} {question}"
