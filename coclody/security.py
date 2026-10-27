"""Lista blanca de chats de Telegram y registro de rechazos.

Política: solo se aceptan chats privados (``chat_type == "private"``) cuyo
chat ID esté en la lista blanca y coincida con el ID del usuario que escribe.
Grupos y canales se rechazan siempre, aunque su ID esté en la lista: en un
grupo cualquier miembro podría dar órdenes.

Cada rechazo se registra en logs/security.log con IDs, tipo de chat, tipo de
update y motivo. El texto del mensaje nunca se registra.

Cualquiera que conozca el usuario del bot le puede escribir. Para que alguien
no pueda empujar los registros viejos fuera de la rotación a fuerza de
mensajes, se registran como máximo ``LOG_BURST`` rechazos por chat cada
``LOG_WINDOW_S`` segundos; el resto se cuenta y se informa en una sola línea.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable, Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SECURITY_LOGGER = "coclody.security"
_MAX_LOG_BYTES = 1_000_000
_LOG_BACKUPS = 5
_LABEL_RE = re.compile(r"[^a-z0-9_]")
LOG_BURST = 5
LOG_WINDOW_S = 600.0
_MAX_TRACKED_CHATS = 1024


def configure_security_log(path: Path) -> logging.Logger:
    """Configura el logger de seguridad para escribir en ``path`` (con rotación).

    Si ya estaba configurado, cierra el handler anterior: llamarlo dos veces no
    duplica líneas.
    """
    logger = logging.getLogger(SECURITY_LOGGER)
    close_security_log()
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False  # que no termine también en el log general
    if os.name == "posix":
        path.chmod(0o600)
    return logger


def close_security_log() -> None:
    logger = logging.getLogger(SECURITY_LOGGER)
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()


class ChatGuard:
    def __init__(
        self,
        allowed_chat_ids: Iterable[int],
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.monotonic,
    ):
        ids = frozenset(allowed_chat_ids)
        if not all(type(i) is int and i > 0 for i in ids):
            raise ValueError("La lista blanca solo admite chat IDs enteros positivos.")
        self._allowed = ids
        self._log = logger or logging.getLogger(SECURITY_LOGGER)
        self._clock = clock
        # chat -> [inicio de la ventana, registrados en la ventana, suprimidos en la ventana]
        self._windows: dict[str, list[float | int]] = {}

    def is_authorized(self, chat_id: Any, user_id: Any, chat_type: Any, update_kind: str = "message") -> bool:
        """True si el update se puede procesar. Si no, lo registra (con límite) y devuelve False."""
        reason = self._rejection_reason(chat_id, user_id, chat_type)
        if reason is None:
            return True
        suppressed = self._take_log_slot(_id_label(chat_id))
        if suppressed is None:
            return False
        if suppressed:
            self._log.warning(
                "suprimidos chat_id=%s rechazos=%d (más de %d en %.0f s)",
                _id_label(chat_id), suppressed, LOG_BURST, LOG_WINDOW_S,
            )
        self._log.warning(
            "rechazado reason=%s chat_id=%s user_id=%s chat_type=%s update=%s",
            reason,
            _id_label(chat_id),
            _id_label(user_id),
            _label(chat_type),
            _label(update_kind),
        )
        return False

    def permits(self, chat_id: Any, user_id: Any, chat_type: Any) -> bool:
        """Lo mismo que is_authorized pero sin registrar nada (para decidir si responder un error)."""
        return self._rejection_reason(chat_id, user_id, chat_type) is None

    def _take_log_slot(self, key: str) -> int | None:
        """None si este rechazo no se registra; si no, cuántos se suprimieron antes en la ventana anterior."""
        now = self._clock()
        window = self._windows.get(key)
        if window is None or now - window[0] >= LOG_WINDOW_S:
            if window is None and len(self._windows) >= _MAX_TRACKED_CHATS:
                # Muchos chats distintos: se descartan las ventanas vencidas, o todas si no alcanza.
                self._windows = {k: w for k, w in self._windows.items() if now - w[0] < LOG_WINDOW_S}
                if len(self._windows) >= _MAX_TRACKED_CHATS:
                    self._windows.clear()
            suppressed = int(window[2]) if window else 0
            self._windows[key] = [now, 1, 0]
            return suppressed
        if window[1] < LOG_BURST:
            window[1] += 1
            return 0
        window[2] += 1
        return None

    def _rejection_reason(self, chat_id: Any, user_id: Any, chat_type: Any) -> str | None:
        if type(chat_id) is not int or type(user_id) is not int:
            return "missing_or_invalid_id"
        if chat_type != "private":
            return "not_private_chat"
        if chat_id != user_id:
            return "user_chat_mismatch"
        if chat_id not in self._allowed:
            return "not_in_whitelist"
        return None


def _id_label(value: Any) -> str:
    return str(value) if type(value) is int else f"<{type(value).__name__}>"


def _label(value: Any) -> str:
    # Solo [a-z0-9_] y largo acotado: un valor externo no puede inyectar líneas en el log.
    if not isinstance(value, str):
        return f"<{type(value).__name__}>"
    return _LABEL_RE.sub("?", value.lower())[:32] or "<empty>"
