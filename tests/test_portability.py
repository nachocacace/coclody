"""Chequeo estático de portabilidad (macOS, Windows y Linux; Python 3.12 a 3.14).

Recorre todo el código (app, tools y tests, que también corren en Windows) y
lista cada uso de APIs de módulos sensibles a la plataforma. Cada una tiene
que estar en PORTABLE (existe y se comporta igual en las tres plataformas y en
3.12+) o en PLATFORM_SPECIFIC, con su disponibilidad y los archivos donde se
permite usarla (que la protegen con un chequeo por ``hasattr``/``os.name``/
``sys.platform`` o un ``skipif``).

Una API nueva sin clasificar hace fallar este test en la máquina de quien la
agrega, sin esperar a CI. Así se habría detectado ``os.waitid`` (no existe en
macOS con Python 3.12 ni en Windows). Clasificar obliga a revisar la
disponibilidad en la documentación; los tests que simulan su ausencia (como
los de tests/test_process.py sin ``os.waitid``) verifican que el código la
proteja de verdad.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULES = {"os", "signal", "select", "subprocess", "ctypes", "shutil", "platform", "time", "stat", "tempfile", "socket"}
SPECIFIC_MODULES = {"fcntl", "termios", "pwd", "grp", "resource", "msvcrt", "winreg", "_winapi", "posix", "nt", "pty",
                    "tty", "curses", "readline", "syslog", "crypt", "winsound"}

PORTABLE = {
    "os.name", "os.environ", "os.sep", "os.pathsep", "os.path", "os.getcwd", "os.open", "os.close", "os.fdopen", "os.replace",
    "os.access", "os.X_OK", "os.W_OK", "os.R_OK", "os.F_OK", "os.O_CREAT", "os.O_EXCL", "os.O_WRONLY", "os.O_TRUNC",
    "os.O_RDONLY", "os.getpid", "os.urandom", "os.fspath",
    "signal.SIGTERM", "signal.SIGINT",
    "subprocess.Popen", "subprocess.PIPE", "subprocess.DEVNULL", "subprocess.STDOUT", "subprocess.run",
    "subprocess.TimeoutExpired", "subprocess.CalledProcessError",
    "ctypes.Structure", "ctypes.byref", "ctypes.sizeof", "ctypes.c_int", "ctypes.c_int64", "ctypes.c_size_t",
    "ctypes.c_ulonglong", "ctypes.c_void_p", "ctypes.c_uint32",
    "shutil.which", "shutil.copyfile", "shutil.rmtree", "shutil.move",
    "platform.machine", "platform.system",
    "time.monotonic", "time.sleep", "time.time", "time.time_ns",
    "stat.S_IMODE",
    "tempfile.TemporaryDirectory",
}

PROCESS = "coclody/orchestrator/process.py"
_WAITID = ("Linux; macOS desde Python 3.13; no existe en Windows", {PROCESS, "tests/test_process.py"})
PLATFORM_SPECIFIC: dict[str, tuple[str, set[str]]] = {
    "os.waitid": _WAITID,
    "os.P_PID": _WAITID,
    "os.WEXITED": _WAITID,
    "os.WNOHANG": _WAITID,
    "os.WNOWAIT": _WAITID,
    "os.killpg": ("solo POSIX", {PROCESS}),
    "os.kill": ("en Windows termina el proceso en vez de mandar una señal", {"tests/test_process.py"}),
    "signal.SIGKILL": ("solo POSIX", {PROCESS}),
    "select.kqueue": ("macOS y BSD", {PROCESS, "tests/test_process.py"}),
    "select.kevent": ("macOS y BSD", {PROCESS}),
    "select.KQ_*": ("macOS y BSD", {PROCESS}),
    "subprocess.CREATE_NEW_PROCESS_GROUP": ("solo Windows", {PROCESS}),
    "subprocess.CREATE_NO_WINDOW": ("solo Windows", {PROCESS}),
    "ctypes.WinDLL": ("solo Windows", {PROCESS, "tests/test_process.py"}),
    "ctypes.windll": ("solo Windows", {"coclody/orchestrator/keepawake.py"}),
    "ctypes.WinError": ("solo Windows", {PROCESS}),
    "ctypes.get_last_error": ("solo Windows", {PROCESS}),
    "ctypes.wintypes": ("pensado para Windows; se importa solo dentro de `if sys.platform == \"win32\"`", {PROCESS}),
    # Métodos de Path con requisitos de plataforma (se detectan por nombre).
    ".symlink_to": ("en Windows requiere privilegios o modo desarrollador",
                    {"tests/test_sandbox.py", "tests/test_orchestrator_setup.py", "tests/test_config.py", "tests/test_paths.py",
                     "tests/test_claude_runner.py"}),
}


def _sources() -> list[Path]:
    files = [*ROOT.joinpath("coclody").rglob("*.py"), *ROOT.joinpath("tools").glob("*.py"),
             *ROOT.joinpath("tests").glob("*.py"), ROOT / "run_coclody.py"]
    return sorted(f for f in files if f.name != "test_portability.py")


def _specific(name: str) -> tuple[str, set[str]] | None:
    if name in PLATFORM_SPECIFIC:
        return PLATFORM_SPECIFIC[name]
    for key, value in PLATFORM_SPECIFIC.items():
        if key.endswith("*") and name.startswith(key[:-1]):
            return value
    return None


def _uses(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
    aliases: dict[str, str] = {}
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                top = a.name.split(".")[0]
                if top in MODULES:
                    aliases[a.asname or top] = top
                if top in SPECIFIC_MODULES:
                    found.append((f"import {a.name}", node.lineno))
        elif isinstance(node, ast.ImportFrom) and node.module:
            top = node.module.split(".")[0]
            if top in SPECIFIC_MODULES:
                found.append((f"import {node.module}", node.lineno))
            elif node.module in MODULES:
                found += [(f"{node.module}.{a.name}", node.lineno) for a in node.names]
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if isinstance(node.value, ast.Name) and node.value.id in aliases:
                found.append((f"{aliases[node.value.id]}.{node.attr}", node.lineno))
            elif node.attr == "symlink_to":
                found.append((".symlink_to", node.lineno))
    return found


def test_every_platform_sensitive_api_is_classified():
    problems = []
    for path in _sources():
        rel = path.relative_to(ROOT).as_posix()
        for name, line in _uses(path):
            if name in PORTABLE:
                continue
            spec = _specific(name)
            if spec is None:
                problems.append(
                    f"{rel}:{line}: {name} no está clasificada. Revisá en la documentación si existe en "
                    f"Windows, macOS y Linux con Python 3.12+: si sí, agregala a PORTABLE; si no, a "
                    f"PLATFORM_SPECIFIC (con su disponibilidad) y protegé su uso."
                )
            elif rel not in spec[1]:
                problems.append(f"{rel}:{line}: {name} ({spec[0]}) solo se permite en {sorted(spec[1])}.")
    assert not problems, "\n".join(problems)


def test_scanner_detects_unclassified_api(tmp_path):
    sample = tmp_path / "x.py"
    sample.write_text("import os as _o\nimport select\nfrom os import fork\n_o.waitid\nselect.epoll\nimport fcntl\n")
    names = {name for name, _ in _uses(sample)}
    assert {"os.waitid", "select.epoll", "os.fork", "import fcntl"} <= names
    assert _specific("select.epoll") is None and "select.epoll" not in PORTABLE
    assert _specific("select.KQ_FILTER_PROC") is not None


def test_registry_has_no_stale_entries():
    used = {name for path in _sources() for name, _ in _uses(path)}
    stale = [k for k in PLATFORM_SPECIFIC if not k.endswith("*") and k not in used]
    assert not stale, f"Entradas de PLATFORM_SPECIFIC que ya no se usan: {stale}"
