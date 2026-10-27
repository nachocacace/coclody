"""Línea de comandos: ``coclody [run|setup|check]``.

Sin subcomando: si todavía no hay config.toml corre el setup; si ya hay, arranca
el bot. Así, en Windows, abrir el ejecutable con doble clic hace lo esperable.
"""

from __future__ import annotations

import argparse
import sys
from contextlib import closing

from coclody import __version__
from coclody.config import ConfigError, load_config
from coclody.credentials import SecretError, SecretName, SecretStore
from coclody.db import SCHEMA_VERSION, connect
from coclody.db.migrations import current_version
from coclody.orchestrator.sandbox import SandboxError, resolve_executable
from coclody.paths import AppPaths, DataDirError, default_paths, is_frozen


def main(argv: list[str] | None = None) -> int:
    _safe_console()
    parser = argparse.ArgumentParser(prog="coclody", description="Orquestador de tareas para Claude Code por Telegram.")
    parser.add_argument("--version", action="version", version=f"CoClody {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="{run,setup,check}")
    sub.add_parser("run", help="arranca el bot (Ctrl+C para salir)")
    sub.add_parser("setup", help="crea la configuración y guarda el token del bot en el llavero")
    sub.add_parser("check", help="verifica configuración, llavero, base y Claude Code sin arrancar el bot")
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
        else:
            from coclody.main import run

            code = run()
    except KeyboardInterrupt:
        print("\nInterrumpido.", file=sys.stderr)
        code = 130
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
