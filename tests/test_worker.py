from __future__ import annotations

import os
import sys
import threading
import time

import pytest

from coclody.config import ClaudeConfig
from coclody.db import connection_factory, repo
from coclody.orchestrator.claude_runner import ClaudeResult
from coclody.orchestrator.worker import (
    CANCEL_SHUTDOWN,
    CANCEL_USER,
    QUESTION_MARKER,
    CancelRegistry,
    Worker,
    build_prompt,
    extract_question,
)

SESSION = "123e4567-e89b-12d3-a456-426614174000"


def result(status="ok", text="Listo, hice X.", session=SESSION, exit_code=0):
    return ClaudeResult(
        status=status,
        exit_code=exit_code,
        session_id=session,
        result_text=text,
        summary=text or status,
        stdout_truncated=False,
        duration_s=0.1,
    )


class FakeRunner:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def __call__(self, inv, cancel):
        self.calls.append(inv)
        item = self.results.pop(0)
        return item(inv, cancel) if callable(item) else item


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "proj").mkdir(parents=True)
    return root


@pytest.fixture
def exe(tmp_path):
    path = tmp_path / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    path.parent.mkdir()
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def db(conn, app_paths):
    return connection_factory(app_paths.db_file)


@pytest.fixture
def make_worker(ws, exe, db):
    def make(*results):
        runner = FakeRunner(*results)
        cfg = ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=("Read",), timeout_minutes=1)
        return Worker(cfg, db, CancelRegistry(), runner), runner

    return make


def queued(conn, title="tarea", **kw):
    t = repo.create_task(conn, title, **kw)
    return repo.transition_task(conn, t.id, "queued")


def test_no_tasks(make_worker):
    worker, runner = make_worker()
    assert worker.run_next() is None
    assert runner.calls == []


def test_happy_path(conn, make_worker, ws):
    t = queued(conn, "hacer algo", description="con detalle", workdir_rel="proj")
    worker, runner = make_worker(result())
    final = worker.run_next()
    assert final.id == t.id and final.status == "done"
    (inv,) = runner.calls
    assert inv.cwd == (ws / "proj").resolve()
    prompt = inv.stdin.decode()
    assert "hacer algo" in prompt and "con detalle" in prompt and QUESTION_MARKER in prompt
    (run,) = repo.list_runs(conn, t.id)
    assert (run.status, run.session_id, run.summary) == ("ok", SESSION, "Listo, hice X.")
    kinds = [(e.kind, e.message) for e in repo.list_events(conn, t.id)]
    assert ("progress", "Listo, hice X.") in kinds
    assert kinds[-1] == ("status_change", "running → done")
    assert [e.message for e in repo.pending_notifications(conn)] == ["running → done"]


def test_priority_order(conn, make_worker):
    low = queued(conn, "baja")
    high = repo.create_task(conn, "alta", priority=10)
    repo.transition_task(conn, high.id, "queued")
    worker, _ = make_worker(result(), result())
    assert worker.run_next().id == high.id
    assert worker.run_next().id == low.id


@pytest.mark.parametrize("status", ["error", "timeout"])
def test_failure(conn, make_worker, status):
    t = queued(conn)
    worker, _ = make_worker(result(status=status, text="se rompió", exit_code=1))
    assert worker.run_next().status == "failed"
    assert repo.list_runs(conn, t.id)[0].status == status
    assert any(e.kind == "error" and e.message == "se rompió" for e in repo.list_events(conn, t.id))


def test_question_then_answer_then_resume(conn, make_worker):
    t = queued(conn, "migrar base")
    worker, runner = make_worker(
        result(text=f"Revisé todo.\n\n{QUESTION_MARKER} ¿Qué motor uso, Postgres o MySQL?"),
        result(text="Migré a Postgres.", session="223e4567-e89b-12d3-a456-426614174000"),
    )
    assert worker.run_next().status == "waiting_input"
    (q,) = repo.list_open_questions(conn, task_id=t.id)
    assert q.question == "¿Qué motor uso, Postgres o MySQL?"
    assert [e.message for e in repo.pending_notifications(conn)] == ["running → waiting_input"]

    assert worker.run_next() is None  # esperando respuesta: no se ejecuta
    repo.answer_question(conn, q.id, "Postgres", answered_by=111)
    repo.transition_task(conn, t.id, "queued", expected="waiting_input")

    assert worker.run_next().status == "done"
    second = runner.calls[1]
    assert second.argv[-2:] == ("--resume", SESSION)
    prompt = second.stdin.decode()
    assert "¿Qué motor uso, Postgres o MySQL?" in prompt and "Respuesta: Postgres" in prompt
    assert len(repo.list_runs(conn, t.id)) == 2


def test_user_cancel_while_running(conn, make_worker):
    t = queued(conn)

    def slow(inv, cancel):
        assert worker.cancels.request(t.id, CANCEL_USER)
        assert cancel.is_set()
        return result(status="killed", text=None, session=None, exit_code=-9)

    worker, _ = make_worker(slow)
    assert worker.run_next().status == "cancelled"
    assert repo.list_runs(conn, t.id)[0].status == "killed"
    assert repo.pending_notifications(conn) == []  # cancelled no se notifica


def test_shutdown_cancel_marks_failed(conn, make_worker):
    t = queued(conn)

    def slow(inv, cancel):
        worker.cancels.cancel_all(CANCEL_SHUTDOWN)
        return result(status="killed", text=None, session=None, exit_code=-9)

    worker, _ = make_worker(slow)
    assert worker.run_next().status == "failed"
    assert any("Interrumpida al cerrar" in e.message for e in repo.list_events(conn, t.id))


def test_registry_is_cleared_after_run(conn, make_worker):
    t = queued(conn)
    worker, _ = make_worker(result())
    worker.run_next()
    assert worker.cancels.request(t.id) is False


def test_sandbox_error_fails_task(conn, make_worker):
    t = queued(conn, workdir_rel="no-existe")
    worker, runner = make_worker()
    final = worker.run_next()
    assert final.status == "failed" and runner.calls == []
    run = repo.list_runs(conn, t.id)[0]
    assert run.status == "error" and "No se pudo lanzar Claude Code" in run.summary


def test_runner_crash_still_closes_everything(conn, make_worker):
    t = queued(conn)

    def boom(inv, cancel):
        raise OSError("exec format error")

    worker, _ = make_worker(boom)
    assert worker.run_next().status == "failed"
    assert repo.list_active_runs(conn) == []
    assert "OSError" in repo.list_runs(conn, t.id)[0].summary


def test_task_cancelled_before_pickup_is_skipped(conn, make_worker, monkeypatch):
    t = queued(conn)
    worker, runner = make_worker(result())
    original = repo.next_queued_task

    def racy(c):
        found = original(c)
        repo.transition_task(c, t.id, "cancelled")  # el usuario cancela justo ahora
        return found

    monkeypatch.setattr(repo, "next_queued_task", racy)
    assert worker.run_next() is None
    assert runner.calls == [] and repo.list_runs(conn, t.id) == []
    assert worker.cancels.request(t.id) is False


def test_recover(conn, make_worker):
    t = queued(conn)
    repo.transition_task(conn, t.id, "running")
    repo.start_run(conn, t.id)
    other = queued(conn, "sigue en cola")
    worker, _ = make_worker()
    assert worker.recover() == 1
    assert repo.get_task(conn, t.id).status == "failed"
    assert repo.list_runs(conn, t.id)[0].status == "killed"
    assert repo.get_task(conn, other.id).status == "queued"
    assert worker.recover() == 0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"bla\n{QUESTION_MARKER} ¿cuál?", "¿cuál?"),
        (f"bla\n{QUESTION_MARKER} ¿cuál?\n\n  ", "¿cuál?"),
        (f"{QUESTION_MARKER} ¿a?\nluego seguí y terminé", None),  # no es la última línea
        (f"bla {QUESTION_MARKER} ¿cuál?", None),  # no empieza la línea
        (f"bla\n{QUESTION_MARKER}   ", None),
        ("", None),
        (None, None),
    ],
)
def test_extract_question(text, expected):
    assert extract_question(text) == expected


def test_extract_question_truncates():
    assert len(extract_question(f"{QUESTION_MARKER} " + "x" * 5000)) == 4000


def test_prompt_does_not_format_user_braces(conn):
    t = repo.create_task(conn, "usar {id} y {marker}", description="{title}")
    prompt = build_prompt(t, [])
    assert "usar {id} y {marker}" in prompt and "\n{title}\n" in prompt


# --- de punta a punta con un claude falso ------------------------------------------------

FAKE = """#!{python}
import json, sys
prompt = sys.stdin.read()
if "Respuesta: azul" in prompt:
    text = "Pinté todo de azul."
else:
    text = "Necesito saber algo.\\n{marker} ¿De qué color?"
print(json.dumps({{"type": "result", "is_error": False, "session_id": "{session}", "result": text}}))
"""


@pytest.mark.skipif(os.name != "posix", reason="el claude falso es un script con shebang")
def test_end_to_end_with_fake_claude(conn, db, ws, tmp_path):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE.format(python=sys.executable, marker=QUESTION_MARKER, session=SESSION))
    exe.chmod(0o755)
    cfg = ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=("Read",), timeout_minutes=1)
    worker = Worker(cfg, db)
    t = queued(conn, "pintar")

    assert worker.run_next().status == "waiting_input"
    (q,) = repo.list_open_questions(conn, task_id=t.id)
    repo.answer_question(conn, q.id, "azul", answered_by=111)
    repo.transition_task(conn, t.id, "queued")
    started = time.monotonic()
    assert worker.run_next().status == "done"
    assert time.monotonic() - started < 10
    assert repo.list_runs(conn, t.id)[-1].summary == "Pinté todo de azul."


@pytest.mark.skipif(os.name != "posix", reason="el claude falso es un script con shebang")
def test_end_to_end_cancel_kills_process(conn, db, ws, tmp_path):
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(60)\n")
    exe.chmod(0o755)
    cfg = ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=("Read",), timeout_minutes=1)
    worker = Worker(cfg, db)
    t = queued(conn)

    def cancel_soon():
        for _ in range(100):
            if worker.cancels.request(t.id, CANCEL_USER):
                return
            time.sleep(0.05)

    threading.Thread(target=cancel_soon).start()
    started = time.monotonic()
    assert worker.run_next().status == "cancelled"
    assert time.monotonic() - started < 10
