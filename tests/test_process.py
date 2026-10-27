from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from coclody.orchestrator.process import ProcessError, run_bounded

PY = sys.executable
posix_only = pytest.mark.skipif(os.name != "posix", reason="verificación de PIDs vía os.kill(pid, 0) solo en POSIX")


def run(code, *args, tmp_path, stdin=b"", timeout=20.0, max_out=100_000, max_err=100_000, env=None, cancel=None):
    return run_bounded(
        [PY, "-c", code, *args],
        cwd=tmp_path,
        env=dict(os.environ) if env is None else env,
        stdin_data=stdin,
        timeout_s=timeout,
        max_stdout=max_out,
        max_stderr=max_err,
        cancel=cancel,
    )


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def wait_dead(pid: int, seconds: float = 5.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if not alive(pid):
            return True
        time.sleep(0.05)
    return False


def test_stdin_stdout_stderr_and_exit_code(tmp_path):
    code = "import sys; d = sys.stdin.read(); print(d.upper()); print('err', file=sys.stderr); sys.exit(3)"
    r = run(code, tmp_path=tmp_path, stdin="hola ñandú".encode())
    assert r.exit_code == 3
    assert r.stdout.decode().strip() == "HOLA ÑANDÚ"
    assert r.stderr.decode().strip() == "err"
    assert not (r.timed_out or r.cancelled or r.stdout_truncated or r.stderr_truncated)


def test_output_is_bounded_without_blocking(tmp_path):
    code = "import sys; sys.stdout.write('x' * 5_000_000); sys.stderr.write('e' * 1_000_000)"
    r = run(code, tmp_path=tmp_path, max_out=1000, max_err=10)
    assert r.exit_code == 0
    assert r.stdout == b"x" * 1000 and r.stdout_truncated
    assert r.stderr == b"e" * 10 and r.stderr_truncated


def test_exact_limit_is_not_truncated(tmp_path):
    r = run("import sys; sys.stdout.write('x' * 100)", tmp_path=tmp_path, max_out=100)
    assert r.stdout == b"x" * 100 and not r.stdout_truncated


def test_large_stdin_not_read_by_child(tmp_path):
    r = run("print('no leo stdin')", tmp_path=tmp_path, stdin=b"y" * 5_000_000)
    assert r.exit_code == 0 and b"no leo" in r.stdout


def test_timeout_kills(tmp_path):
    started = time.monotonic()
    r = run("import time; time.sleep(60)", tmp_path=tmp_path, timeout=1)
    assert r.timed_out and not r.cancelled
    assert r.exit_code != 0
    assert time.monotonic() - started < 15


GRANDCHILD = """
import subprocess, sys, time
p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
open(sys.argv[1], "w").write(str(p.pid))
{after}
"""


@posix_only
def test_timeout_kills_grandchildren(tmp_path):
    pidfile = tmp_path / "pid"
    r = run(GRANDCHILD.format(after="time.sleep(60)"), str(pidfile), tmp_path=tmp_path, timeout=2)
    assert r.timed_out
    assert wait_dead(int(pidfile.read_text()))


@posix_only
def test_orphan_holding_pipes_is_killed_after_normal_exit(tmp_path):
    pidfile = tmp_path / "pid"
    started = time.monotonic()
    r = run(GRANDCHILD.format(after="print('listo')"), str(pidfile), tmp_path=tmp_path, timeout=30)
    assert r.exit_code == 0 and not r.timed_out
    assert b"listo" in r.stdout
    assert time.monotonic() - started < 20
    assert wait_dead(int(pidfile.read_text()))


def test_cancel(tmp_path):
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    r = run("import time; time.sleep(60)", tmp_path=tmp_path, timeout=30, cancel=cancel)
    assert r.cancelled and not r.timed_out
    assert r.duration_s < 15


def test_cwd_and_env_are_exactly_what_was_given(tmp_path):
    env = {"SOLO_ESTA": "1", **({"SYSTEMROOT": os.environ["SYSTEMROOT"]} if "SYSTEMROOT" in os.environ else {})}
    code = "import os, json; print(json.dumps([os.getcwd(), sorted(os.environ)]))"
    r = run(code, tmp_path=tmp_path, env=env)
    import json

    cwd, keys = json.loads(r.stdout)
    assert os.path.samefile(cwd, tmp_path)
    assert "SOLO_ESTA" in keys and "HOME" not in keys and "PATH" not in keys


@pytest.mark.parametrize(
    "payload",
    ["; touch PWNED", "$(touch PWNED)", "`touch PWNED`", "&& echo PWNED > PWNED", "| echo PWNED > PWNED", "%PATH% & echo PWNED > PWNED", "\n touch PWNED"],
)
def test_arguments_are_never_interpreted_by_a_shell(tmp_path, payload):
    r = run("import sys, json; print(json.dumps(sys.argv[1:]))", payload, tmp_path=tmp_path)
    import json

    assert json.loads(r.stdout) == [payload]
    assert not (tmp_path / "PWNED").exists()


@pytest.mark.parametrize(
    "argv",
    [[], ["python"], [PY, "a\x00b"], [PY, 5], "no-es-lista"],
)
def test_argv_validation(tmp_path, argv):
    with pytest.raises((ProcessError, TypeError)):
        run_bounded(
            argv, cwd=tmp_path, env=os.environ, stdin_data=b"", timeout_s=5, max_stdout=10, max_stderr=10
        )


def test_timeout_must_be_positive(tmp_path):
    with pytest.raises(ProcessError):
        run_bounded([PY, "-c", "pass"], cwd=tmp_path, env=os.environ, stdin_data=b"", timeout_s=0, max_stdout=1, max_stderr=1)
