"""Arranque de CoClody: config, secretos, base, recuperación y bot."""

from __future__ import annotations

import logging
import sys
from contextlib import closing

from coclody.config import ConfigError, load_config
from coclody.credentials import SecretError, SecretName, SecretStore
from coclody.db import connect, connection_factory, migrate
from coclody.logging_setup import configure_app_log
from coclody.orchestrator.worker import Worker
from coclody.paths import DataDirError, default_paths
from coclody.security import ChatGuard, configure_security_log
from coclody.telegram_bot.app import Services, build_application, run_polling

log = logging.getLogger("coclody")


def run() -> int:
    """Corre el bot hasta Ctrl+C. Devuelve el código de salida."""
    try:
        paths = default_paths()
    except DataDirError as exc:
        print(exc, file=sys.stderr)
        return 2
    paths.ensure()
    configure_security_log(paths.security_log)
    try:
        cfg = load_config(paths)
        token = SecretStore().require(SecretName.TELEGRAM_BOT_TOKEN)
    except (ConfigError, SecretError) as exc:
        print(exc, file=sys.stderr)
        return 2
    configure_app_log(paths.app_log, secrets=[token])

    with closing(connect(paths.db_file)) as conn:
        migrate(conn)
    db = connection_factory(paths.db_file)
    worker = Worker(cfg.claude, db)
    recovered = worker.recover()
    if recovered:
        log.warning("Se marcaron como fallidas %d tareas que quedaron en ejecución", recovered)

    services = Services(config=cfg, db=db, worker=worker, guard=ChatGuard(cfg.telegram.allowed_chat_ids))
    log.info("CoClody arrancó; datos en %s", paths.data_dir)
    run_polling(build_application(token, services))
    return 0
