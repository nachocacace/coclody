"""Ejecución de un proceso con timeout, cancelación, salida acotada y kill del árbol.

- Siempre lista de argumentos y ``shell=False``. ``argv[0]`` tiene que ser absoluto.
- stdout y stderr se leen en hilos hasta el final (para que el proceso nunca
  se bloquee con el pipe lleno), pero solo se guardan los primeros N bytes.
- Al terminar, siempre (salida normal, timeout o cancelación) se mata lo que
  quede del árbol: Claude Code lanza subprocesos (shells, node, etc.).
  - POSIX: el hijo arranca en su propia sesión (``start_new_session``). Su
    salida se detecta sin cosecharlo: mientras el líder es un zombie su ID de
    grupo no se puede reutilizar, así que el ``killpg`` final nunca le pega a
    un grupo ajeno. Si hubo timeout o cancelación se manda SIGTERM, se dan
    unos segundos y luego SIGKILL. Cómo se detecta la salida sin cosechar
    depende de lo que ofrezca el intérprete (se elige por ``hasattr``, no por
    nombre de plataforma; ver ``_exit_mode``):
      * ``os.waitid(WNOWAIT)``: Linux siempre; macOS desde Python 3.13.
      * ``select.kqueue`` con ``NOTE_EXIT``: macOS con Python 3.12, BSD.
      * Si no hay ninguno de los dos, ``poll()`` (que sí cosecha): el árbol se
        mata igual, pero entre la cosecha y el ``killpg`` queda una ventana
        mínima en la que el ID de grupo, si el grupo ya estaba vacío, podría
        reutilizarse. Ninguna plataforma soportada cae en este caso.
  - Windows: el hijo se asigna a un Job Object con KILL_ON_JOB_CLOSE (vía
    ctypes, sin comandos externos); terminar el job mata a todos sus
    procesos. Limitación: un subproceso lanzado en los primeros instantes,
    antes de la asignación, podría quedar fuera del job.
"""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

_CHUNK = 64 * 1024
_TERM_GRACE_S = 3.0
_READER_JOIN_S = 5.0


class ProcessError(Exception):
    pass


@dataclass(frozen=True)
class ProcessResult:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    cancelled: bool
    duration_s: float


class _Capture:
    def __init__(self, limit: int):
        self.limit = limit
        self.data = bytearray()
        self.truncated = False

    def drain(self, stream: IO[bytes]) -> None:
        try:
            while chunk := stream.read1(_CHUNK):
                room = self.limit - len(self.data)
                if room > 0:
                    self.data += chunk[:room]
                if len(chunk) > max(room, 0):
                    self.truncated = True
        except (OSError, ValueError):
            pass  # pipe cerrado al matar el proceso


def run_bounded(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdin_data: bytes,
    timeout_s: float,
    max_stdout: int,
    max_stderr: int,
    cancel: threading.Event | None = None,
    poll_interval_s: float = 0.1,
) -> ProcessResult:
    args = list(argv)
    if not args or not all(isinstance(a, str) for a in args):
        raise ProcessError("argv tiene que ser una lista no vacía de strings")
    if any("\x00" in a for a in args):
        raise ProcessError("argv no puede contener caracteres NUL")
    if not Path(args[0]).is_absolute():
        raise ProcessError("argv[0] tiene que ser una ruta absoluta")
    if timeout_s <= 0:
        raise ProcessError("timeout_s tiene que ser positivo")

    kwargs: dict = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW

    started = time.monotonic()
    proc = subprocess.Popen(
        args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(cwd),
        env=dict(env),
        shell=False,
        close_fds=True,
        **kwargs,
    )
    watcher = _ExitWatcher(proc)
    job = None
    if sys.platform == "win32":
        try:
            job = _WindowsJob(proc)
        except OSError as exc:
            proc.kill()
            proc.wait()
            watcher.close()
            raise ProcessError(f"no se pudo crear el Job Object de Windows: {exc}") from None

    out, err = _Capture(max_stdout), _Capture(max_stderr)
    threads = [
        threading.Thread(target=out.drain, args=(proc.stdout,), daemon=True),
        threading.Thread(target=err.drain, args=(proc.stderr,), daemon=True),
        threading.Thread(target=_feed, args=(proc.stdin, stdin_data), daemon=True),
    ]
    for t in threads:
        t.start()

    timed_out = cancelled = False
    deadline = started + timeout_s
    try:
        while not watcher.exited():
            if time.monotonic() >= deadline:
                timed_out = True
                break
            if cancel is not None and cancel.is_set():
                cancelled = True
                break
            time.sleep(poll_interval_s)
    finally:
        _kill_tree(proc, job, watcher, graceful=not watcher.exited())
        proc.wait()  # recién acá se cosecha al líder
        watcher.close()
        if job is not None:
            job.close()
        for t in threads:
            t.join(_READER_JOIN_S)

    return ProcessResult(
        exit_code=proc.returncode,
        stdout=bytes(out.data),
        stderr=bytes(err.data),
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_s=time.monotonic() - started,
    )


def _feed(stream: IO[bytes], data: bytes) -> None:
    try:
        stream.write(data)
    except OSError:
        pass  # el proceso terminó sin leer todo
    finally:
        try:
            stream.close()
        except OSError:
            pass


def _exit_mode() -> str:
    """Cómo detectar la salida del hijo. Por funcionalidad disponible, no por nombre de SO."""
    if os.name != "posix":
        return "poll"  # Windows: el Job Object cubre el árbol; cosechar antes no importa
    if hasattr(os, "waitid") and hasattr(os, "WNOWAIT"):
        return "waitid"
    if hasattr(select, "kqueue"):
        return "kqueue"
    return "poll"


class _ExitWatcher:
    """Detecta que el hijo terminó. En los modos waitid y kqueue no lo cosecha."""

    def __init__(self, proc: subprocess.Popen, mode: str | None = None):
        self._proc = proc
        self.mode = _exit_mode() if mode is None else mode
        self._exited = False
        self._kq = None
        if self.mode == "kqueue":
            self._kq = select.kqueue()
            event = select.kevent(
                proc.pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                fflags=select.KQ_NOTE_EXIT,
            )
            try:
                self._kq.control([event], 0, 0)
            except ProcessLookupError:
                # Ya terminó antes de registrarlo. No puede ser otro proceso con el
                # mismo PID: el hijo todavía no fue cosechado, sigue siendo un zombie.
                self._exited = True

    def exited(self) -> bool:
        if self._exited:
            return True
        if self.mode == "waitid":
            try:
                self._exited = os.waitid(os.P_PID, self._proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
            except ChildProcessError:
                self._exited = True
        elif self.mode == "kqueue":
            self._exited = bool(self._kq.control(None, 1, 0))
        else:
            self._exited = self._proc.poll() is not None
        return self._exited

    def close(self) -> None:
        if self._kq is not None:
            self._kq.close()
            self._kq = None


def _kill_tree(proc: subprocess.Popen, job: _WindowsJob | None, watcher: _ExitWatcher, graceful: bool) -> None:
    """Mata todo el árbol. Con ``graceful`` primero pide terminar (solo POSIX)."""
    if job is not None:
        job.terminate()
        return
    if graceful:
        _killpg(proc.pid, signal.SIGTERM)
        end = time.monotonic() + _TERM_GRACE_S
        while not watcher.exited() and time.monotonic() < end:
            time.sleep(0.05)
    _killpg(proc.pid, signal.SIGKILL)


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _JobObjectExtendedLimitInformation = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

    class _IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_ulonglong)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class _BasicLimits(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class _ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", _BasicLimits),
            ("IoInfo", _IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL

    class _WindowsJob:
        def __init__(self, proc: subprocess.Popen):
            handle = _kernel32.CreateJobObjectW(None, None)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            self._handle = handle
            info = _ExtendedLimits()
            info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            ok = _kernel32.SetInformationJobObject(
                handle, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
            )
            if not ok or not _kernel32.AssignProcessToJobObject(handle, int(proc._handle)):
                error = ctypes.get_last_error()
                self.close()
                raise ctypes.WinError(error)

        def terminate(self) -> None:
            if self._handle:
                _kernel32.TerminateJobObject(self._handle, 1)

        def close(self) -> None:
            if self._handle:
                _kernel32.CloseHandle(self._handle)
                self._handle = None

else:

    class _WindowsJob:  # solo para el type checker; nunca se instancia fuera de Windows
        def __init__(self, proc: subprocess.Popen):
            raise OSError("Job Objects solo existen en Windows")

        def terminate(self) -> None: ...

        def close(self) -> None: ...
