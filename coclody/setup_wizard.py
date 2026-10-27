"""``coclody setup``: crea config.toml y guarda el token del bot en el llavero.

- Nunca sobreescribe un config.toml existente (lo valida y muestra problemas).
- La config se valida con las mismas reglas que al arrancar, antes de quedar
  en su lugar: se escribe a un archivo temporal y se reemplaza solo si es válida.
- El token se pide sin eco y no se imprime nunca. Si ya hay uno guardado, se
  conserva salvo que se pida explícitamente reemplazarlo.
- No usa la red: no verifica el token contra Telegram.
"""

from __future__ import annotations

import getpass
import os
from collections.abc import Callable
from pathlib import Path

from coclody.config import TEMPLATE, ConfigError, load_config
from coclody.credentials import SecretError, SecretName, SecretStore
from coclody.paths import AppPaths

MAX_ATTEMPTS = 5
TOKEN = SecretName.TELEGRAM_BOT_TOKEN

Ask = Callable[[str], str]
Out = Callable[[str], None]


class SetupAborted(Exception):
    pass


def run_setup(
    paths: AppPaths,
    store: SecretStore,
    *,
    ask: Ask = input,
    ask_secret: Ask = getpass.getpass,
    out: Out = print,
) -> int:
    """Asistente interactivo. Devuelve 0 si todo quedó listo, 1 si no."""
    paths.ensure()
    try:
        config_ok = _setup_config(paths, ask, out)
        token_ok = _setup_token(store, ask, ask_secret, out)
    except (SetupAborted, EOFError):
        out("\nSetup cancelado. No se guardó nada más.")
        return 1
    if config_ok and token_ok:
        out("\nListo. Arrancá CoClody con: coclody run")
        return 0
    out("\nQuedaron cosas por resolver (ver arriba). Podés volver a correr: coclody setup")
    return 1


def render_config(chat_id: int, workspace: str) -> str:
    """Plantilla con los dos valores obligatorios completos."""
    text = TEMPLATE
    for old, new in (
        ("allowed_chat_ids = []", f"allowed_chat_ids = [{int(chat_id)}]"),
        ('workspace_root = ""', f"workspace_root = {toml_string(workspace)}"),
    ):
        if text.count(old) != 1:
            raise RuntimeError(f"La plantilla de config cambió: no encuentro {old!r}")
        text = text.replace(old, new)
    return text


def toml_string(value: str) -> str:
    """String básico de TOML. No sirve json.dumps: escapa lo no-BMP como pares
    sustitutos (inválidos en TOML) y deja sin escapar U+007F."""
    out = []
    for ch in value:
        if ch in ('"', "\\"):
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04X}")
        else:
            out.append(ch)
    return '"' + "".join(out) + '"'


def _setup_config(paths: AppPaths, ask: Ask, out: Out) -> bool:
    target = paths.config_file
    if target.exists():
        out(f"Ya existe {target}; no lo modifico.")
        try:
            load_config(paths)
        except ConfigError as exc:
            out(str(exc))
            out("Corregí el archivo a mano (o borralo y volvé a correr el setup).")
            return False
        out("La configuración es válida.")
        return True

    out(f"Voy a crear {target}.")
    chat_id = _ask_chat_id(ask, out)
    temp = target.with_name(target.name + ".tmp")
    try:
        for _ in range(MAX_ATTEMPTS):
            raw = ask("Carpeta de trabajo para Claude Code (ruta absoluta, tiene que existir): ").strip()
            if not raw:
                raise SetupAborted
            _write_private(temp, render_config(chat_id, str(Path(raw).expanduser())))
            try:
                load_config(paths, path=temp)
            except ConfigError as exc:
                out("Esa configuración no es válida:")
                out("\n".join(f"  - {p}" for p in exc.problems))
                continue
            os.replace(temp, target)
            out(f"Config guardada en {target}.")
            return True
    finally:
        temp.unlink(missing_ok=True)
    out("Demasiados intentos; no se creó la configuración.")
    return False


def _ask_chat_id(ask: Ask, out: Out) -> int:
    out("Tu chat ID de Telegram: escribile a @userinfobot y te lo responde (es un número).")
    for _ in range(MAX_ATTEMPTS):
        raw = ask("Chat ID: ").strip()
        if not raw:
            raise SetupAborted
        if raw.isdigit() and 0 < int(raw) < 2**53:
            return int(raw)
        out("Tiene que ser un número entero positivo (los grupos no se admiten).")
    raise SetupAborted


def _setup_token(store: SecretStore, ask: Ask, ask_secret: Ask, out: Out) -> bool:
    try:
        existing = store.get(TOKEN)
    except SecretError as exc:
        out(f"No pude acceder al llavero del sistema: {exc}")
        return False
    if existing:
        answer = ask("Ya hay un token del bot guardado en el llavero. ¿Reemplazarlo? [s/N]: ").strip().lower()
        if answer not in ("s", "si", "sí", "y", "yes"):
            out("Conservo el token existente.")
            return True
    out("Token del bot: lo da @BotFather al crear el bot. No se muestra mientras lo escribís.")
    for _ in range(MAX_ATTEMPTS):
        token = ask_secret("Token: ").strip()
        if not token:
            raise SetupAborted
        try:
            store.set(TOKEN, token)
        except SecretError as exc:
            out(str(exc))
            continue
        out("Token guardado en el llavero del sistema.")
        return True
    out("Demasiados intentos; no se guardó el token.")
    return False


def _write_private(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
