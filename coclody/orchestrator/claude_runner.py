"""Invocación de Claude Code en modo no interactivo (``claude -p``).

Defensas contra inyección de argumentos:
- El prompt (que viene de Telegram) va por stdin, nunca en argv. Un título
  como ``--dangerously-skip-permissions`` es solo texto.
- Todo lo que sí va en argv sale de la config (validada de nuevo acá) o es
  constante. El session_id para ``--resume`` tiene que ser un UUID.
- Si el ejecutable es un ``.cmd``/``.bat`` (instalación por npm en Windows),
  Windows lo corre a través de cmd.exe, que interpreta ``& | < > ^ " %``.
  Para ese caso cada argumento tiene que pertenecer a un conjunto seguro.

Defensas contra que Claude Code haga más de lo configurado:
- ``--tools`` limita las herramientas disponibles a las de ``allowed_tools``,
  y ``--allowedTools`` las preaprueba con su alcance.
- ``--permission-mode dontAsk`` y ``--permission-prompts none``: lo que no
  esté preaprobado se deniega, nadie lo aprueba.
- ``--strict-mcp-config`` sin ``--mcp-config``: ningún servidor MCP.
- ``--setting-sources user``: no se cargan los settings del proyecto ni los
  locales, que vienen dentro de la carpeta de trabajo y podrían traer hooks
  o permisos.
- Del entorno se quitan las variables que harían usar una API paga
  (``ANTHROPIC_*``, ``CLAUDE_CODE_USE_*``) y las de CoClody.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from coclody.config import ClaudeConfig, is_valid_tool_spec
from coclody.orchestrator.process import run_bounded
from coclody.orchestrator.sandbox import SandboxError, resolve_executable, resolve_workdir

MAX_PROMPT_CHARS = 100_000
MAX_STDOUT_BYTES = 2_000_000
MAX_STDERR_BYTES = 200_000
SUMMARY_MAX_CHARS = 10_000
STDERR_TAIL_CHARS = 2_000

_SESSION_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_BATCH_SAFE_RE = re.compile(r"^[A-Za-z0-9 _.,:/\\*()=@+-]*$")
_ENV_DROP_PREFIXES = ("ANTHROPIC_", "CLAUDE_CODE_USE_", "COCLODY_")


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    stdin: bytes
    timeout_s: float


@dataclass(frozen=True)
class ClaudeResult:
    status: str  # mismo vocabulario que runs.status: ok, error, timeout, killed
    exit_code: int | None
    session_id: str | None
    result_text: str | None
    summary: str
    stdout_truncated: bool
    duration_s: float


def build_invocation(
    cfg: ClaudeConfig,
    prompt: str,
    workdir_rel: str | None = None,
    resume_session: str | None = None,
    base_env: Mapping[str, str] | None = None,
) -> Invocation:
    """Arma (y valida) todo lo necesario para lanzar Claude Code. No ejecuta nada."""
    text = _validate_prompt(prompt)
    workdir = resolve_workdir(cfg.workspace_root, workdir_rel)
    exe = resolve_executable(cfg.executable, cfg.workspace_root)

    tools = list(dict.fromkeys(cfg.allowed_tools))
    bad = [t for t in tools if not is_valid_tool_spec(t)]
    if bad:
        raise SandboxError(f"herramientas inválidas en la config: {', '.join(map(repr, bad))}")
    base_names = list(dict.fromkeys(t.split("(", 1)[0] for t in tools))

    argv = [
        str(exe),
        "-p",
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--strict-mcp-config",
        "--setting-sources", "user",
        "--tools", ",".join(base_names),  # "" deshabilita todas
    ]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    if resume_session is not None:
        if not isinstance(resume_session, str) or not _SESSION_RE.fullmatch(resume_session):
            raise SandboxError("session_id inválido para --resume")
        argv += ["--resume", resume_session]

    if exe.suffix.lower() in (".cmd", ".bat"):
        unsafe = [a for a in argv[1:] if not _BATCH_SAFE_RE.fullmatch(a)]
        if unsafe:
            raise SandboxError(
                "el ejecutable es un script .cmd/.bat y estos argumentos no son seguros para cmd.exe: "
                + ", ".join(map(repr, unsafe))
            )

    return Invocation(
        argv=tuple(argv),
        cwd=workdir,
        env=_child_env(os.environ if base_env is None else base_env),
        stdin=text.encode("utf-8"),
        timeout_s=cfg.timeout_minutes * 60,
    )


def run_claude(inv: Invocation, cancel: threading.Event | None = None) -> ClaudeResult:
    proc = run_bounded(
        inv.argv,
        cwd=inv.cwd,
        env=inv.env,
        stdin_data=inv.stdin,
        timeout_s=inv.timeout_s,
        max_stdout=MAX_STDOUT_BYTES,
        max_stderr=MAX_STDERR_BYTES,
        cancel=cancel,
    )
    stderr_tail = proc.stderr.decode("utf-8", errors="replace")[-STDERR_TAIL_CHARS:].strip()
    parsed = None if proc.stdout_truncated else _parse_result(proc.stdout)
    session_id = parsed.get("session_id") if parsed else None
    if not (isinstance(session_id, str) and _SESSION_RE.fullmatch(session_id)):
        session_id = None
    result_text = parsed.get("result") if parsed else None
    if not isinstance(result_text, str):
        result_text = None

    if proc.timed_out:
        status, summary = "timeout", f"Se cortó por timeout a los {inv.timeout_s / 60:g} minutos."
    elif proc.cancelled:
        status, summary = "killed", "Cancelada."
    elif proc.stdout_truncated:
        status, summary = "error", f"La salida superó {MAX_STDOUT_BYTES} bytes y se descartó."
    elif parsed is None:
        status, summary = "error", "La salida de Claude Code no es el JSON esperado."
    elif proc.exit_code != 0 or parsed.get("is_error") is not False:
        status, summary = "error", result_text or f"Claude Code terminó con código {proc.exit_code}."
    else:
        status, summary = "ok", result_text or "(sin texto de resultado)"

    if status != "ok" and stderr_tail:
        summary = f"{summary}\n\nstderr:\n{stderr_tail}"
    if len(summary) > SUMMARY_MAX_CHARS:
        summary = summary[: SUMMARY_MAX_CHARS - 1] + "…"

    return ClaudeResult(
        status=status,
        exit_code=proc.exit_code,
        session_id=session_id,
        result_text=result_text,
        summary=summary,
        stdout_truncated=proc.stdout_truncated,
        duration_s=proc.duration_s,
    )


def _validate_prompt(prompt: object) -> str:
    if not isinstance(prompt, str) or not prompt.strip():
        raise SandboxError("el prompt no puede estar vacío")
    if len(prompt) > MAX_PROMPT_CHARS:
        raise SandboxError(f"el prompt supera los {MAX_PROMPT_CHARS} caracteres")
    if "\x00" in prompt:
        raise SandboxError("el prompt no puede contener caracteres NUL")
    return prompt


def _child_env(base: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in base.items() if not k.upper().startswith(_ENV_DROP_PREFIXES)}


def _parse_result(stdout: bytes) -> dict | None:
    try:
        data = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("type") != "result":
        return None
    return data
