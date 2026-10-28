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
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coclody.paths import AppPaths

# Alcance de archivos de Claude Code: "./**" es la carpeta de trabajo (el cwd) y todo lo que
# contiene. Sin alcance, Read/Edit/Write llegan a cualquier archivo del usuario: verificado con
# Claude Code 2.1.304 (con "Write" a secas, Claude creó un archivo fuera de la carpeta). Con
# "(./**)" se deniegan rutas absolutas de afuera, "../" y symlinks que apuntan afuera.
WORKSPACE_SCOPE = "(./**)"
FILE_TOOLS = ("Read", "Glob", "Grep", "Edit", "Write", "MultiEdit", "NotebookEdit")
DEFAULT_ALLOWED_TOOLS = tuple(f"{tool}{WORKSPACE_SCOPE}" for tool in ("Read", "Glob", "Grep", "Edit", "Write"))
# Sin comas: --allowedTools las usa como separador.
_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\([^(),\r\n]*\))?$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# Comandos que un worker puede correr con Bash en el modo orquestador: prefijos (tests, linters, build).
_COMMAND_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/:=+-]*( [A-Za-z0-9._/:=+-]+){0,7}")
# Nunca en la lista: git (los commits los hace CoClody), shells, elevación de permisos, red y
# programas que ejecutan otro comando. Un intérprete solo se acepta con un módulo o script detrás.
FORBIDDEN_COMMANDS = frozenset({
    "git", "gh", "sh", "bash", "zsh", "fish", "dash", "ksh", "csh", "tcsh", "cmd", "cmd.exe", "powershell",
    "powershell.exe", "pwsh", "env", "sudo", "su", "doas", "runas", "ssh", "scp", "sftp", "rsync", "curl",
    "wget", "nc", "ncat", "netcat", "telnet", "ftp", "xargs", "eval", "exec", "source", "nohup", "timeout",
    "watch", "rm", "rmdir", "del", "chmod", "chown", "open", "start", "osascript", "crontab", "launchctl",
    "schtasks", "docker", "kubectl",
})
INTERPRETERS = frozenset({"python", "python3", "py", "node", "deno", "bun", "ruby", "perl", "php"})
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
# Herramientas que Claude Code puede usar. "(./**)" las limita a la carpeta de trabajo.
# Cualquier ampliación (quitar el "(./**)", agregar Bash, WebFetch, etc.) le da a Claude acceso
# a más cosas de tu computadora: leé "Qué puede tocar Claude" en el README antes de cambiarlo.
allowed_tools = ["Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./**)", "Write(./**)"]
timeout_minutes = 60
# Límite de salida que se guarda por ejecución (el resto se descarta).
max_stdout_bytes = 2000000
max_stderr_bytes = 200000

[updates]
enabled = false
# Repo público de GitHub con los releases, formato "usuario/repo".
# github_repo = ""
check_interval_hours = 24

[orquestador]
# Modo v2: un cerebro coordina varios workers, cada uno en su worktree de git del proyecto activo.
# Apagado, CoClody funciona como siempre (un worker, en la carpeta de trabajo).
enabled = false
workers = 3
# Comandos que los workers pueden correr (tests, linters, build), como prefijos: "pytest", "npm test".
# git nunca: los commits los hace CoClody. Cualquier comando que corra código del proyecto (tests,
# build) puede hacer cualquier cosa con tus permisos: leé "Orquestador: qué está garantizado".
worker_commands = []
# Topes. Al tocar uno, CoClody avisa por Telegram y la autonomía queda en pausa.
brain_calls_per_hour = 6
brain_calls_per_day = 40
autonomous_tasks_per_day = 20
max_queue_per_worker = 3
# Simulacro: el cerebro decide y te muestra qué haría, sin ejecutar nada.
brain_dry_run = true
prompt_budget_chars = 48000
# Resumen periódico (mecánico, sin costo): cada N tareas o M minutos con actividad. 0 lo apaga.
digest_every_tasks = 5
digest_every_minutes = 120
# Aviso de "cambio grande" en una tarea.
big_change_files = 20
big_change_lines = 1000
# Web local de solo lectura (127.0.0.1, con token en la URL).
web_enabled = false
web_port = 8765
audit_retention_days = 90
# Tope de ejecuciones por tarea: una que falla o toca el límite en loop no se reintenta para siempre.
max_task_runs = 5
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
class OrchestratorConfig:
    enabled: bool = False
    workers: int = 3
    worker_commands: tuple[str, ...] = ()
    brain_calls_per_hour: int = 6
    brain_calls_per_day: int = 40
    autonomous_tasks_per_day: int = 20
    max_queue_per_worker: int = 3
    brain_dry_run: bool = True
    prompt_budget_chars: int = 48_000
    digest_every_tasks: int = 5
    digest_every_minutes: int = 120
    big_change_files: int = 20
    big_change_lines: int = 1000
    web_enabled: bool = False
    web_port: int = 8765
    audit_retention_days: int = 90
    max_task_runs: int = 5


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig
    claude: ClaudeConfig
    updates: UpdatesConfig
    orchestrator: OrchestratorConfig = OrchestratorConfig()


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
    "orquestador": {
        "enabled", "workers", "worker_commands", "brain_calls_per_hour", "brain_calls_per_day",
        "autonomous_tasks_per_day", "max_queue_per_worker", "brain_dry_run", "prompt_budget_chars",
        "digest_every_tasks", "digest_every_minutes", "big_change_files", "big_change_lines", "web_enabled",
        "web_port", "audit_retention_days", "max_task_runs",
    },
}

# Clave → (validador, default) de [orquestador]. Los rangos son los límites duros: ni el cerebro ni
# Telegram pueden cambiarlos, solo este archivo.
_ORCH = {
    "enabled": (lambda: _bool, False),
    "workers": (lambda: _int_between(1, 8), 3),
    "worker_commands": (lambda: _commands, []),
    "brain_calls_per_hour": (lambda: _int_between(1, 60), 6),
    "brain_calls_per_day": (lambda: _int_between(1, 500), 40),
    "autonomous_tasks_per_day": (lambda: _int_between(0, 500), 20),
    "max_queue_per_worker": (lambda: _int_between(1, 20), 3),
    "brain_dry_run": (lambda: _bool, True),
    "prompt_budget_chars": (lambda: _int_between(8_000, 200_000), 48_000),
    "digest_every_tasks": (lambda: _int_between(0, 1000), 5),
    "digest_every_minutes": (lambda: _int_between(0, 24 * 60), 120),
    "big_change_files": (lambda: _int_between(1, 100_000), 20),
    "big_change_lines": (lambda: _int_between(1, 10_000_000), 1000),
    "web_enabled": (lambda: _bool, False),
    "web_port": (lambda: _int_between(1024, 65535), 8765),
    "audit_retention_days": (lambda: _int_between(1, 3650), 90),
    "max_task_runs": (lambda: _int_between(1, 100), 5),
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
    orch_raw = sections["orquestador"]
    orch = {
        key: v.get(orch_raw, f"orquestador.{key}", check(), default=default)
        for key, (check, default) in _ORCH.items()
    }
    # Bash de los workers solo con el sandbox de Claude Code disponible (macOS/Linux); nunca en Windows,
    # donde un comando correría sin contención. Un comando puede ejecutar código arbitrario (conftest.py,
    # package.json), así que sin sandbox la única opción segura es no tener Bash.
    if orch["worker_commands"] and not _sandbox_platform():
        v.error("orquestador.worker_commands",
                "esta plataforma no tiene el sandbox de Claude Code; dejá la lista vacía (los workers no "
                "pueden correr comandos acá, solo leer y editar archivos)")

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
        orchestrator=OrchestratorConfig(**{**orch, "worker_commands": tuple(orch["worker_commands"])}),
    )


def is_valid_tool_spec(spec: object) -> bool:
    """Nombre de herramienta de Claude Code, opcionalmente acotado: ``Read``, ``Read(./**)``.

    ``Bash`` sin acotar no es válido. Lo usan la config y el runner.
    """
    return isinstance(spec, str) and bool(_TOOL_RE.fullmatch(spec)) and spec != "Bash"


def tool_warnings(tools: tuple[str, ...] | list[str]) -> list[str]:
    """Avisos por cada herramienta que amplía lo que Claude puede tocar respecto del default."""
    warnings = []
    for spec in tools:
        base = spec.split("(", 1)[0]
        if base in FILE_TOOLS and spec != f"{base}{WORKSPACE_SCOPE}":
            warnings.append(f"{spec}: puede acceder a archivos fuera de la carpeta de trabajo")
        elif base == "Bash":
            warnings.append(
                f"{spec}: ejecuta comandos con tus permisos; si además Edit o Write están habilitados, "
                "Claude puede escalarlo a cualquier comando (p. ej. vía la configuración del proyecto)"
            )
        elif base not in FILE_TOOLS:
            warnings.append(f"{spec}: herramienta fuera del default; revisá qué habilita")
    return warnings


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
        raise _Invalid(
            '"Bash" sin restricción no está permitido. Incluso acotado, Bash se puede escalar a cualquier '
            "comando si Edit o Write están habilitados: leé el README antes de agregarlo"
        )
    bad = [t for t in value if not is_valid_tool_spec(t)]
    if bad:
        raise _Invalid(f"nombres inválidos: {', '.join(map(repr, bad))}")
    return value


def _commands(value: Any) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(c, str) for c in value):
        raise _Invalid("tiene que ser una lista de comandos")
    problems = []
    for command in value:
        problem = command_problem(command)
        if problem:
            problems.append(f"{command!r}: {problem}")
    if problems:
        raise _Invalid("; ".join(problems))
    if len(set(value)) != len(value):
        raise _Invalid("hay comandos repetidos")
    return value


def _sandbox_platform() -> bool:
    # Igual que pool.sandbox_available, pero sin importar el orquestador (evita un import circular en config).
    return sys.platform in ("darwin", "linux")


def command_problem(command: str) -> str | None:
    """Por qué un comando no puede ir en worker_commands, o None si puede."""
    if not _COMMAND_RE.fullmatch(command):
        return "solo letras, números, espacios simples y . _ / : = + - (sin comillas, ; | & $ ni redirecciones)"
    words = command.split(" ")
    first = words[0].lower().rsplit("/", 1)[-1]
    if first in FORBIDDEN_COMMANDS:
        return "no está permitido (git, shells, red, permisos o comandos que ejecutan otros)"
    if first in INTERPRETERS:
        if len(words) < 2 or words[1] in ("-c", "-e", "--eval", "-p", "--print", "-i", "-"):
            return "un intérprete solo con un módulo o script detrás (p. ej. \"python -m pytest\")"
        if words[1] == "-m" and (len(words) < 3 or words[2] in ("pip", "venv", "http.server")):
            return "ese módulo no es un comando de tests, lint ni build"
    return None


def _repo(value: Any) -> str:
    if not isinstance(value, str) or not _REPO_RE.fullmatch(value):
        raise _Invalid('tiene que tener el formato "usuario/repo"')
    return value
