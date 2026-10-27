"""Lectura y validación de config.toml.

La validación es estricta: claves desconocidas, tipos incorrectos y valores
fuera de rango son errores. Se reportan todos juntos, con la clave completa
(``seccion.clave``), para corregir el archivo de una sola vez.

La lista blanca de Telegram vive acá y no en la base: así no se puede
modificar desde un chat, ni siquiera desde uno autorizado.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coclody.paths import AppPaths

DEFAULT_ALLOWED_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write")
# Sin comas: --allowedTools las usa como separador.
_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\([^(),\r\n]*\))?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REQUIRED = object()

TEMPLATE = """\
# Configuración de CoClody. Este archivo es local: no lo subas al repo.
# El token del bot NO va acá: se guarda en el llavero del sistema con `coclody setup`.

[telegram]
# Chat IDs autorizados (enteros positivos: solo chats privados).
# Para saber el tuyo, escribile a @userinfobot en Telegram.
allowed_chat_ids = []

[claude]
# Carpeta raíz donde Claude Code puede trabajar. Tiene que existir.
# No puede ser tu carpeta personal ni contener los datos de CoClody.
workspace_root = ""
# Nombre del ejecutable (se busca en el PATH) o ruta absoluta.
executable = "claude"
# Herramientas que Claude Code puede usar sin pedir permiso.
# "Bash" sin restricción no está permitido; usá la forma acotada, p. ej. "Bash(git status:*)".
allowed_tools = ["Read", "Glob", "Grep", "Edit", "Write"]
timeout_minutes = 60
# Límite de salida que se guarda por ejecución (el resto se descarta).
max_stdout_bytes = 2000000
max_stderr_bytes = 200000

[updates]
enabled = false
# Repo público de GitHub con los releases, formato "usuario/repo".
# github_repo = ""
check_interval_hours = 24
"""


class ConfigError(Exception):
    def __init__(self, path: Path, problems: list[str]):
        self.path = path
        self.problems = problems
        detail = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"Configuración inválida en {path}:\n{detail}")


@dataclass(frozen=True)
class TelegramConfig:
    allowed_chat_ids: frozenset[int]


@dataclass(frozen=True)
class ClaudeConfig:
    workspace_root: Path
    executable: str
    allowed_tools: tuple[str, ...]
    timeout_minutes: int
    max_stdout_bytes: int = 2_000_000
    max_stderr_bytes: int = 200_000


@dataclass(frozen=True)
class UpdatesConfig:
    enabled: bool
    github_repo: str | None
    check_interval_hours: int


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    claude: ClaudeConfig
    updates: UpdatesConfig


_SCHEMA = {
    "telegram": {"allowed_chat_ids"},
    "claude": {
        "workspace_root",
        "executable",
        "allowed_tools",
        "timeout_minutes",
        "max_stdout_bytes",
        "max_stderr_bytes",
    },
    "updates": {"enabled", "github_repo", "check_interval_hours"},
}


def load_config(paths: AppPaths, path: Path | None = None) -> Config:
    """Lee y valida la configuración. Lanza ConfigError con todos los problemas."""
    path = paths.config_file if path is None else path
    if not path.is_file():
        raise ConfigError(path, ["el archivo no existe; corré `coclody setup` para crearlo"])
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, [f"no es TOML válido: {exc}"]) from None
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(path, [f"no se pudo leer: {exc}"]) from None

    v = _Validator()
    sections = {name: v.section(raw, name) for name in _SCHEMA}
    for name in raw.keys() - _SCHEMA.keys():
        v.error(name, "sección desconocida")

    tg, cl, up = sections["telegram"], sections["claude"], sections["updates"]
    chat_ids = v.get(tg, "telegram.allowed_chat_ids", _chat_ids)
    workspace = v.get(cl, "claude.workspace_root", lambda x: _workspace(x, paths))
    executable = v.get(cl, "claude.executable", _executable, default="claude")
    tools = v.get(cl, "claude.allowed_tools", _tools, default=list(DEFAULT_ALLOWED_TOOLS))
    timeout = v.get(cl, "claude.timeout_minutes", _int_between(1, 24 * 60), default=60)
    max_stdout = v.get(cl, "claude.max_stdout_bytes", _int_between(64 * 1024, 50_000_000), default=2_000_000)
    max_stderr = v.get(cl, "claude.max_stderr_bytes", _int_between(4 * 1024, 10_000_000), default=200_000)
    enabled = v.get(up, "updates.enabled", _bool, default=False)
    repo = v.get(up, "updates.github_repo", _repo, default=None)
    interval = v.get(up, "updates.check_interval_hours", _int_between(1, 24 * 30), default=24)
    if enabled is True and repo is None and "github_repo" not in up:
        v.error("updates.github_repo", "es obligatorio si updates.enabled = true")

    if v.problems:
        raise ConfigError(path, v.problems)
    return Config(
        telegram=TelegramConfig(allowed_chat_ids=frozenset(chat_ids)),
        claude=ClaudeConfig(
            workspace_root=workspace,
            executable=executable,
            allowed_tools=tuple(tools),
            timeout_minutes=timeout,
            max_stdout_bytes=max_stdout,
            max_stderr_bytes=max_stderr,
        ),
        updates=UpdatesConfig(enabled=enabled, github_repo=repo, check_interval_hours=interval),
    )


def is_valid_tool_spec(spec: object) -> bool:
    """Nombre de herramienta de Claude Code, opcionalmente acotado: ``Read``, ``Bash(git status:*)``.

    ``Bash`` sin acotar no es válido. Lo usan la config y el runner.
    """
    return isinstance(spec, str) and bool(_TOOL_RE.fullmatch(spec)) and spec != "Bash"


def write_template(path: Path) -> None:
    """Crea config.toml con la plantilla. Nunca sobreescribe uno existente."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="\n") as fh:
        fh.write(TEMPLATE)
    if os.name == "posix":
        path.chmod(0o600)


# --- validación ---------------------------------------------------------------


class _Invalid(ValueError):
    pass


class _Validator:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def error(self, key: str, message: str) -> None:
        self.problems.append(f"{key}: {message}")

    def section(self, raw: dict[str, Any], name: str) -> dict[str, Any]:
        value = raw.get(name, {})
        if not isinstance(value, dict):
            self.error(name, "tiene que ser una sección [" + name + "]")
            return {}
        for key in value.keys() - _SCHEMA[name]:
            self.error(f"{name}.{key}", "clave desconocida")
        return value

    def get(self, section: dict[str, Any], key: str, check, default: Any = _REQUIRED) -> Any:
        name = key.split(".", 1)[1]
        if name not in section:
            if default is _REQUIRED:
                self.error(key, "falta (es obligatorio)")
            return default
        try:
            return check(section[name])
        except _Invalid as exc:
            self.error(key, f"{exc} (valor actual: {_show(section[name])})")
            return None


def _show(value: Any) -> str:
    text = repr(value)
    return text if len(text) <= 80 else text[:77] + "..."


def _is_int(value: Any) -> bool:
    return type(value) is int  # bool es subclase de int y no se acepta


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise _Invalid("tiene que ser true o false")
    return value


def _int_between(low: int, high: int):
    def check(value: Any) -> int:
        if not _is_int(value) or not low <= value <= high:
            raise _Invalid(f"tiene que ser un entero entre {low} y {high}")
        return value

    return check


def _chat_ids(value: Any) -> list[int]:
    if not isinstance(value, list) or not value:
        raise _Invalid("tiene que ser una lista no vacía de chat IDs")
    if not all(_is_int(x) and x > 0 for x in value):
        raise _Invalid("cada chat ID tiene que ser un entero positivo (solo chats privados)")
    return value


def _workspace(value: Any, paths: AppPaths) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise _Invalid("tiene que ser la ruta de una carpeta")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise _Invalid("tiene que ser una ruta absoluta")
    if not path.is_dir():
        raise _Invalid("la carpeta no existe")
    path = path.resolve()
    if path == Path.home().resolve():
        raise _Invalid("no puede ser tu carpeta personal entera")
    for app_dir in paths.all_dirs():
        app_dir = app_dir.resolve()
        if path.is_relative_to(app_dir) or app_dir.is_relative_to(path):
            raise _Invalid(f"no puede contener ni estar dentro de la carpeta de CoClody ({app_dir})")
    return path


def _executable(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _Invalid("tiene que ser un nombre de ejecutable o una ruta absoluta")
    has_separator = "/" in value or "\\" in value
    if has_separator and not Path(value).is_absolute():
        raise _Invalid("si es una ruta, tiene que ser absoluta")
    return value


def _tools(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
        raise _Invalid("tiene que ser una lista de nombres de herramientas")
    if "Bash" in value:
        raise _Invalid('"Bash" sin restricción no está permitido; usá la forma acotada, p. ej. "Bash(git status:*)"')
    bad = [t for t in value if not is_valid_tool_spec(t)]
    if bad:
        raise _Invalid(f"nombres inválidos: {', '.join(map(repr, bad))}")
    return value


def _repo(value: Any) -> str:
    if not isinstance(value, str) or not _REPO_RE.fullmatch(value):
        raise _Invalid('tiene que tener el formato "usuario/repo"')
    return value
