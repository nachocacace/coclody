"""Log general de la app (logs/coclody.log, con rotación) sin filtrar secretos.

- httpx/httpcore se suben a WARNING: a nivel INFO loguean cada URL de la API
  de Telegram, que incluye el token del bot.
- Además, todo registro pasa por ``RedactFilter``, que reemplaza los secretos
  conocidos por ``<redacted>`` por si alguno aparece en un mensaje o traceback.
  De un token de bot (``<id>:<secreto>``) también se busca la parte secreta
  sola, así se tapa aunque el token aparezca codificado (``%3A`` por ``:``).
- ``redact()`` aplica lo mismo a texto que no pasa por logging (p. ej. un
  error que la CLI muestra por consola).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path

_MAX_LOG_BYTES = 2_000_000
_LOG_BACKUPS = 5
_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_QUIET_LOGGERS = ("httpx", "httpcore")
_MIN_SECRET_PART = 20
_registered: tuple[str, ...] = ()


def _variants(secrets: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for secret in secrets:
        if not secret:
            continue
        out.append(secret)
        _, sep, tail = secret.partition(":")
        if sep and len(tail) >= _MIN_SECRET_PART:
            out.append(tail)
    # Los más largos primero: así el token completo se reemplaza entero.
    return tuple(sorted(set(out), key=len, reverse=True))


def redact(text: str) -> str:
    """Reemplaza los secretos registrados con configure_app_log por ``<redacted>``."""
    for secret in _registered:
        text = text.replace(secret, "<redacted>")
    return text


class RedactFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = _variants(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        message = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        redacted, exc_text = message, record.exc_text
        for secret in self._secrets:
            redacted = redacted.replace(secret, "<redacted>")
            if exc_text:
                exc_text = exc_text.replace(secret, "<redacted>")
        if redacted != message:
            record.msg, record.args = redacted, None
        record.exc_text = exc_text
        return True


def configure_app_log(path: Path, secrets: Iterable[str] = (), console: bool = True) -> logging.Logger:
    global _registered
    secrets = tuple(secrets)
    _registered = _variants(secrets)
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_coclody", False)]:
        root.removeHandler(handler)
        handler.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    redact_filter = RedactFilter(secrets)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler._coclody = True  # para poder reconfigurar sin duplicar
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(redact_filter)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if os.name == "posix":
        path.chmod(0o600)
    return root
