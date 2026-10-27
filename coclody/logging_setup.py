"""Log general de la app (logs/coclody.log, con rotación) sin filtrar secretos.

- httpx/httpcore se suben a WARNING: a nivel INFO loguean cada URL de la API
  de Telegram, que incluye el token del bot.
- Además, todo registro pasa por ``RedactFilter``, que reemplaza los secretos
  conocidos por ``<redacted>`` por si alguno aparece en un mensaje o traceback.
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


class RedactFilter(logging.Filter):
    def __init__(self, secrets: Iterable[str]):
        super().__init__()
        self._secrets = tuple(s for s in secrets if s)

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
    root = logging.getLogger()
    for handler in [h for h in root.handlers if getattr(h, "_coclody", False)]:
        root.removeHandler(handler)
        handler.close()

    path.parent.mkdir(parents=True, exist_ok=True)
    redact = RedactFilter(secrets)
    handlers: list[logging.Handler] = [
        RotatingFileHandler(path, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8")
    ]
    if console:
        handlers.append(logging.StreamHandler(sys.stderr))
    for handler in handlers:
        handler._coclody = True  # para poder reconfigurar sin duplicar
        handler.setFormatter(logging.Formatter(_FORMAT))
        handler.addFilter(redact)
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    for name in _QUIET_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    if os.name == "posix":
        path.chmod(0o600)
    return root
