"""Línea de comandos: ``coclody [run|setup|check|update]``.

Sin subcomando: si todavía no hay config.toml corre el setup; si ya hay, arranca
el bot. Así, en Windows, abrir el ejecutable con doble clic hace lo esperable.
"""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import closing

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
    sub = parser.add_subparsers(dest="command", metavar="{run,setup,check,update}")
    sub.add_parser("run", help="arranca el bot (Ctrl+C para salir)")
    sub.add_parser("setup", help="crea la configuración y guarda el token del bot en el llavero")
    sub.add_parser("check", help="verifica configuración, llavero, base y Claude Code sin arrancar el bot")
    sub.add_parser("update", help="busca una versión nueva firmada y, si corre como ejecutable, la instala")
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
