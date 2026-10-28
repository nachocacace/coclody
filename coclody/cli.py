"""Línea de comandos: ``coclody [run|setup|check|update|proyecto]``.

Sin subcomando: si todavía no hay config.toml corre el setup; si ya hay, arranca
el bot. Así, en Windows, abrir el ejecutable con doble clic hace lo esperable.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing
from pathlib import Path

from coclody import __version__
from coclody.config import ConfigError, load_config, tool_warnings
from coclody.credentials import SecretError, SecretName, SecretStore
from coclody.db import SCHEMA_VERSION, connect
from coclody.db.migrations import current_version
from coclody.orchestrator.sandbox import SandboxError, resolve_executable
from coclody.paths import AppPaths, DataDirError, default_paths, is_frozen


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    parser = argparse.ArgumentParser(prog="coclody", description="Orquestador de tareas para Claude Code por Telegram.")
    parser.add_argument("--version", action="version", version=f"CoClody {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{run,setup,check,update,proyecto}")
    sub.add_parser("run", help="arranca el bot (Ctrl+C para salir)")
    sub.add_parser("setup", help="crea la configuración y guarda el token del bot en el llavero")
    sub.add_parser("check", help="verifica configuración, llavero, base y Claude Code sin arrancar el bot")
    sub.add_parser("update", help="busca una versión nueva firmada y, si corre como ejecutable, la instala")
    proj = sub.add_parser("proyecto", help="proyectos del orquestador: agregar, listar, activar")
    proj_sub = proj.add_subparsers(dest="accion", required=True, metavar="{agregar,listar,activar}")
    add = proj_sub.add_parser("agregar", help="registra un proyecto (un repo de git dentro de la carpeta de trabajo)")
    add.add_argument("nombre")
    add.add_argument("ruta", help="carpeta raíz del repo (ruta absoluta)")
    add.add_argument("--activar", action="store_true", help="además lo deja como proyecto activo")
    proj_sub.add_parser("listar", help="lista los proyectos y cuál está activo")
    act = proj_sub.add_parser("activar", help="cambia el proyecto activo (crea los worktrees que falten)")
    act.add_argument("nombre")
    args = parser.parse_args(argv)
    bare = args.command is None and argv is None and len(sys.argv) <= 1

    try:
        paths = default_paths()
    except DataDirError as exc:
        print(exc, file=sys.stderr)
        return _finish(2, bare)

    command = args.command
    if command is None:
        command = "run" if paths.config_file.exists() else "setup"
    try:
        if command == "setup":
            from coclody.setup_wizard import run_setup

            code = run_setup(paths, SecretStore())
        elif command == "check":
            code = check(paths, SecretStore())
        elif command == "update":
            code = update(paths)
        elif command == "proyecto":
            code = project_command(paths, args)
        else:
            from coclody.main import run

            code = run()
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        code = 130
    except Exception as exc:
        # Sin esto el traceback salía por consola sin pasar por la redacción de secretos
        # (p. ej. InvalidToken de python-telegram-bot incluye el token en el mensaje).
        from coclody.logging_setup import redact

        logging.getLogger("coclody").exception("Error inesperado")
        print(redact(f"Error inesperado: {type(exc).__name__}: {exc}"), file=sys.stderr)
        print(f"El detalle quedó en el log: {paths.app_log}", file=sys.stderr)
        code = 1
    return _finish(code, bare)


def check(paths: AppPaths, store: SecretStore, out=print) -> int:
    """Diagnóstico sin efectos: no migra la base ni arranca nada. 0 si está todo bien."""
    ok = True
    out(f"CoClody {__version__}{' (ejecutable)' if is_frozen() else ''}")
    out(f"Datos: {paths.data_dir}")
    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        out(f"[ERROR] {exc}")
        return 2
    out(f"[ok] Configuración válida; carpeta de trabajo: {cfg.claude.workspace_root}")
    warnings = tool_warnings(cfg.claude.allowed_tools)
    if warnings:
        for warning in warnings:
            out(f"[aviso] Herramienta ampliada: {warning}")
    else:
        out("[ok] Herramientas limitadas a la carpeta de trabajo")

    try:
        token = store.get(SecretName.TELEGRAM_BOT_TOKEN)
    except SecretError as exc:
        out(f"[ERROR] Llavero: {exc}")
        ok = False
    else:
        if token:
            out("[ok] Token del bot presente en el llavero")
        else:
            out("[ERROR] Falta telegram_bot_token en el llavero; corré `coclody setup`.")
            ok = False

    try:
        exe = resolve_executable(cfg.claude.executable, cfg.claude.workspace_root)
    except SandboxError as exc:
        out(f"[ERROR] Claude Code: {exc}")
        ok = False
    else:
        out(f"[ok] Claude Code: {exe}")

    from coclody.updater.pubkey import release_verify_key  # también confirma que PyNaCl carga

    if release_verify_key() is None:
        out("[aviso] Sin clave pública de releases: las actualizaciones automáticas no instalan nada")
    else:
        out("[ok] Clave pública de releases configurada")

    if cfg.orchestrator.enabled:
        ok = _check_orchestrator(paths, cfg, out) and ok
    else:
        out("[ok] Orquestador (modo v2): desactivado")

    if paths.db_file.exists():
        with closing(connect(paths.db_file)) as conn:
            version = current_version(conn)
        if version > SCHEMA_VERSION:
            out(f"[ERROR] La base es de una versión más nueva de CoClody (esquema v{version}).")
            ok = False
        else:
            out(f"[ok] Base de datos: esquema v{version} (se actualiza a v{SCHEMA_VERSION} al arrancar)")
    else:
        out("[ok] Base de datos: se crea al arrancar")
    return 0 if ok else 2


def _check_orchestrator(paths: AppPaths, cfg, out) -> bool:
    from coclody.db import projects as prepo
    from coclody.orchestrator.gitops import Git, GitError
    from coclody.orchestrator.projects import ProjectError, inspect

    out(f"[ok] Orquestador (modo v2): activado, {cfg.orchestrator.workers} workers")
    if cfg.orchestrator.worker_commands:
        out("[aviso] Comandos de los workers: " + ", ".join(cfg.orchestrator.worker_commands))
        out("[aviso] Esos comandos corren código del proyecto: las reglas de git quedan garantizadas solo por "
            "instrucción (ver \"Orquestador: qué está garantizado\" en el README)")
    try:
        git = Git.find(cfg.claude.workspace_root, paths.git_hooks_dir)
    except GitError as exc:
        out(f"[ERROR] {exc}")
        return False
    out(f"[ok] git: {git.executable}")
    if not paths.db_file.exists():
        out("[aviso] Todavía no hay proyectos: `coclody proyecto agregar <nombre> <ruta>`")
        return True
    with closing(connect(paths.db_file)) as conn:
        if current_version(conn) < 6:
            out("[aviso] La base se actualiza al arrancar; después agregá un proyecto")
            return True
        project = prepo.active_project(conn)
    if project is None:
        out("[aviso] No hay proyecto activo: `coclody proyecto activar <nombre>`")
        return True
    try:
        reports = inspect(git, cfg, project)
    except ProjectError as exc:
        out(f"[ERROR] Proyecto {project.name}: {exc}")
        return False
    out(f"[ok] Proyecto activo: {project.name} (rama base {project.base_branch})")
    ok = True
    for report in reports:
        tag = {"ok": "[ok]", "missing": "[aviso]"}.get(report.status, "[ERROR]")
        out(f"{tag} Worktree {report.spec.name} ({report.spec.branch}): {report.problem or 'bien'}")
        ok = ok and report.status != "error"
    try:
        has_identity = git.has_identity(project.root_path)
    except GitError:
        has_identity = False
    if has_identity:
        out("[ok] Identidad de git configurada (los commits de CoClody la usan)")
    else:
        out("[ERROR] git no tiene user.name/user.email en este repo; los commits de los workers fallarían. "
            "Configuralos con: git -C <repo> config user.name \"...\" y user.email \"...\"")
        ok = False
    return ok


def project_command(paths: AppPaths, args, out=print) -> int:
    """``coclody proyecto agregar|listar|activar``. 0 si salió bien."""
    from coclody.db import connection_factory, migrate
    from coclody.db import projects as prepo
    from coclody.orchestrator import projects
    from coclody.orchestrator.gitops import Git, GitError

    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        out(str(exc))
        return 2
    if not cfg.orchestrator.enabled:
        out("[aviso] El orquestador está desactivado ([orquestador] enabled = false): el proyecto se guarda, "
            "pero CoClody no lo usa hasta que lo actives.")
    paths.ensure()
    with closing(connect(paths.db_file)) as conn:
        migrate(conn)
    db = connection_factory(paths.db_file)
    if args.accion == "listar":
        with db() as conn:
            rows = prepo.list_projects(conn)
        if not rows:
            out("No hay proyectos. Agregá uno con `coclody proyecto agregar <nombre> <ruta>`.")
        for row in rows:
            out(f"{'*' if row.active else ' '} {row.name}  {row.root}  (rama base {row.base_branch})")
        return 0
    try:
        git = Git.find(cfg.claude.workspace_root, paths.git_hooks_dir)
        if args.accion == "agregar":
            project = projects.add(db, git, cfg, args.nombre, Path(args.ruta).expanduser())
            out(f"[ok] Proyecto {project.name} registrado: {project.root} (rama base {project.base_branch})")
            if not args.activar:
                return 0
        activation = projects.activate(db, git, cfg, args.nombre)
    except GitError as exc:
        out(f"[ERROR] {exc}")
        return 2
    except projects.ProjectError as exc:
        out(f"[ERROR] {exc}")
        for report in exc.reports:
            out(f"  {report.spec.name}: {report.status}{': ' + report.problem if report.problem else ''}")
        return 2
    out(f"[ok] Proyecto activo: {activation.project.name}")
    for report in activation.reports:
        out(f"  {report.spec.name} ({report.spec.branch}): {'creado' if report.status == 'created' else 'bien'}")
    return 0


def update(paths: AppPaths, out=print, updater_factory=None) -> int:
    """Chequeo manual. 0: al día / disponible / instalada; 1: sin red; 2: error o deshabilitado."""
    from coclody.updater.client import Updater

    try:
        cfg = load_config(paths)
    except ConfigError as exc:
        out(str(exc))
        return 2
    if not cfg.updates.github_repo:
        out("Falta updates.github_repo en la configuración.")
        return 2
    factory = updater_factory or (lambda repo, d: Updater(repo, d))
    result = factory(cfg.updates.github_repo, paths.updates_dir).check()
    out(result.message)
    return {"up_to_date": 0, "available": 0, "installed": 0, "offline": 1}.get(result.status, 2)


def _safe_console() -> None:
    # En una consola de Windows redirigida (p. ej. CI) la codificación puede no
    # cubrir todos los caracteres: mejor un "?" que un UnicodeEncodeError.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass


def _finish(code: int, bare: bool) -> int:
    # Ejecutable de Windows abierto con doble clic: que la ventana no se cierre sin poder leer nada.
    if bare and is_frozen() and sys.platform == "win32":
        try:
            input("\nPresioná Enter para cerrar...")
        except EOFError:
            pass
    return code
