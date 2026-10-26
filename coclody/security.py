"""Lista blanca de chats de Telegram y registro de rechazos.

Política: solo se aceptan chats privados (``chat_type == "private"``) cuyo
chat ID esté en la lista blanca y coincida con el ID del usuario que escribe.
Grupos y canales se rechazan siempre, aunque su ID esté en la lista: en un
grupo cualquier miembro podría dar órdenes.

Cada rechazo se registra en logs/security.log con IDs, tipo de chat, tipo de
update y motivo. El texto del mensaje nunca se registra.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

SECURITY_LOGGER = "coclody.security"
_MAX_LOG_BYTES = 1_000_000
_LOG_BACKUPS = 5
_LABEL_RE = re.compile(r"[^a-z0-9_]")


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
    def __init__(self, allowed_chat_ids: Iterable[int], logger: logging.Logger | None = None):
        ids = frozenset(allowed_chat_ids)
        if not all(type(i) is int and i > 0 for i in ids):
            raise ValueError("La lista blanca solo admite chat IDs enteros positivos.")
        self._allowed = ids
        self._log = logger or logging.getLogger(SECURITY_LOGGER)

    def is_authorized(self, chat_id: Any, user_id: Any, chat_type: Any, update_kind: str = "message") -> bool:
        """True si el update se puede procesar. Si no, lo registra y devuelve False."""
        reason = self._rejection_reason(chat_id, user_id, chat_type)
        if reason is None:
            return True
        self._log.warning(
            "rechazado reason=%s chat_id=%s user_id=%s chat_type=%s update=%s",
            reason,
            _id_label(chat_id),
            _id_label(user_id),
            _label(chat_type),
            _label(update_kind),
        )
        return False

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
