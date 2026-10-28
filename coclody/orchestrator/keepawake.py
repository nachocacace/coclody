"""Evita que la computadora se duerma mientras el orquestador está activo.

Si la Mac (o Windows) se suspende, mata los procesos de Claude Code en curso y deja tareas a medias.
- macOS: ``caffeinate -i -w <pid>`` se ata al proceso de CoClody y se corta solo cuando CoClody termina.
- Windows: ``SetThreadExecutionState`` con ES_SYSTEM_REQUIRED mientras CoClody corre.
- Otras plataformas: no se hace nada (no pasa nada si no está disponible).

Solo evita la suspensión del sistema, no apaga la pantalla ni toca ninguna otra configuración.
"""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import sys

log = logging.getLogger(__name__)

_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None

    def __enter__(self) -> KeepAwake:
        self.start()
        return self

    def __exit__(self, *exc) -> None:
        self.stop()

    def start(self) -> None:
        if sys.platform == "darwin":
            self._start_caffeinate()
        elif sys.platform == "win32":
            self._set_windows(_ES_CONTINUOUS | _ES_SYSTEM_REQUIRED)

    def stop(self) -> None:
        if self._proc is not None:
            with contextlib.suppress(Exception):
                self._proc.terminate()
            self._proc = None
        if sys.platform == "win32":
            self._set_windows(_ES_CONTINUOUS)

    def _start_caffeinate(self) -> None:
        from coclody.orchestrator.sandbox import find_in_path

        exe = find_in_path("caffeinate")
        if exe is None:
            log.info("caffeinate no está disponible; la Mac podría dormirse durante tareas largas")
            return
        try:
            self._proc = subprocess.Popen(
                [str(exe), "-i", "-w", str(os.getpid())],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.warning("No se pudo iniciar caffeinate: %s", exc)

    def _set_windows(self, flags: int) -> None:
        try:
            import ctypes

            ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - solo Windows
            log.warning("No se pudo evitar la suspensión en Windows: %s", exc)
