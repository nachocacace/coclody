"""Arranque de CoClody: config, secretos, base, recuperación y bot."""

from __future__ import annotations

import logging
import sys
from contextlib import closing

from coclody.config import ConfigError, load_config, tool_warnings
from coclody.credentials import SecretError, SecretName, SecretStore
from coclody.db import connect, connection_factory, migrate
from coclody.logging_setup import configure_app_log
from coclody.orchestrator.worker import Worker
from coclody.paths import DataDirError, default_paths, executable_path
from coclody.security import ChatGuard, configure_security_log
from coclody.telegram_bot.app import Services, build_application, run_polling
from coclody.updater.client import Updater
from coclody.updater.installer import cleanup_previous

log = logging.getLogger("coclody")


def prepare_orchestrator(cfg, paths, db):
    """Con el orquestador activado: busca git y deja listos los worktrees del proyecto activo.

    Un problema se registra como ERROR y no impide arrancar: el bot sigue
    respondiendo (p. ej. para cambiar de proyecto con /proyecto).
    """
    if not cfg.orchestrator.enabled:
        return None
    from coclody.db import projects as prepo
    from coclody.orchestrator import projects as project_ops
    from coclody.orchestrator.gitops import Git, GitError

    try:
        git = Git.find(cfg.claude.workspace_root, paths.git_hooks_dir)
    except GitError as exc:
        log.error("Orquestador sin git: %s", exc)
        return None
    with db() as conn:
        project = prepo.active_project(conn)
    if project is None:
        log.warning("Orquestador activado sin proyecto activo: `coclody proyecto activar <nombre>`")
        return git
    try:
        reports = project_ops.prepare(db, git, cfg, project)
    except project_ops.ProjectError as exc:
        log.error("Proyecto %s: %s", project.name, exc)
        for report in exc.reports:
            if report.problem:
                log.error("Worktree %s: %s", report.spec.name, report.problem)
        return git
    with db() as conn:
        prepo.reset_worker_states(conn, project.id)  # recover() ya cerró lo que quedó ejecutando
    created = [r.spec.name for r in reports if r.status == "created"]
    log.info("Proyecto activo: %s; worktrees listos%s", project.name, f" (creados: {', '.join(created)})" if created else "")
    return git


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
    for leftover in cleanup_previous(executable_path(), paths.updates_dir):
        log.info("Borrado resto de una actualización anterior: %s", leftover)

    with closing(connect(paths.db_file)) as conn:
        migrate(conn)
    db = connection_factory(paths.db_file)
    worker = Worker(cfg.claude, db)
    recovered = worker.recover()
    if recovered:
        log.warning("Se marcaron como fallidas %d tareas que quedaron en ejecución", recovered)

    updater = None
    if cfg.updates.enabled and cfg.updates.github_repo:
        updater = Updater(cfg.updates.github_repo, paths.updates_dir)
    services = Services(
        config=cfg, db=db, worker=worker, guard=ChatGuard(cfg.telegram.allowed_chat_ids), updater=updater,
        git=prepare_orchestrator(cfg, paths, db), app_paths=paths,
    )
    for warning in tool_warnings(cfg.claude.allowed_tools):
        log.warning("Herramienta ampliada respecto del default: %s", warning)
    log.info("CoClody arrancó; datos en %s", paths.data_dir)
    run_polling(build_application(token, services))
    return 0
