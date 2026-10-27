from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from coclody.db import SCHEMA_VERSION, connect, migrate, repo, transaction
from coclody.db.migrations import SchemaTooNewError, current_version
from coclody.db.repo import InvalidTransitionError, NotFoundError, ValidationError

# --- conexión y migraciones -----------------------------------------------------


def test_migrate_is_idempotent(conn):
    assert current_version(conn) == SCHEMA_VERSION
    assert migrate(conn) == SCHEMA_VERSION
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"tasks", "questions", "runs", "task_events"} <= tables


def test_pragmas(conn):
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.in_transaction is False


def test_refuses_newer_schema(app_paths):
    c = connect(app_paths.db_file)
    c.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(SchemaTooNewError):
        migrate(c)
    c.close()


def test_failed_migration_rolls_back(app_paths, monkeypatch):
    from coclody.db import migrations

    monkeypatch.setattr(migrations, "MIGRATIONS", ("CREATE TABLE a (x INTEGER);\nCREATE TABLE a (x INTEGER);",))
    monkeypatch.setattr(migrations, "SCHEMA_VERSION", 1)
    c = connect(app_paths.db_file)
    with pytest.raises(sqlite3.OperationalError):
        migrations.migrate(c)
    assert current_version(c) == 0
    assert c.execute("SELECT count(*) FROM sqlite_master WHERE name = 'a'").fetchone()[0] == 0
    c.close()


@pytest.mark.skipif(os.name != "posix", reason="permisos POSIX")
def test_db_file_is_private(conn, app_paths):
    repo.create_task(conn, "x")
    for suffix in ("", "-wal", "-shm"):
        f = app_paths.db_file.with_name(app_paths.db_file.name + suffix)
        if f.exists():
            assert stat.S_IMODE(f.stat().st_mode) == 0o600, f


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with transaction(conn):
            conn.execute("INSERT INTO tasks (title) VALUES ('x')")
            raise RuntimeError
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == 0
    assert conn.in_transaction is False


def test_strict_tables_reject_wrong_types(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tasks (title, priority) VALUES ('x', 'alta')")


# --- tasks --------------------------------------------------------------------


def test_create_and_get_task(conn):
    t = repo.create_task(conn, "  Hacer algo  ", description="detalle", priority=5, workdir_rel="proj/a", created_by=111)
    assert (t.title, t.description, t.priority, t.workdir_rel, t.created_by) == ("Hacer algo", "detalle", 5, "proj/a", 111)
    assert t.status == "pending" and t.finished_at is None and t.created_at
    assert repo.get_task(conn, t.id) == t
    assert repo.get_task(conn, 9999) is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"title": ""},
        {"title": "   "},
        {"title": "x" * 201},
        {"title": 5},
        {"title": "ok", "priority": 101},
        {"title": "ok", "priority": True},
        {"title": "ok", "description": "x" * 10001},
        {"title": "ok", "created_by": 0},
    ],
)
def test_create_task_validation(conn, kwargs):
    with pytest.raises(ValidationError):
        repo.create_task(conn, **kwargs)
    assert repo.list_tasks(conn) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("proj", "proj"),
        ("proj/sub/", "proj/sub"),
        (r"proj\sub", "proj/sub"),
        ("./proj//sub/.", "proj/sub"),
        ("", None),
        (".", None),
        (None, None),
    ],
)
def test_workdir_normalization(value, expected):
    assert repo.normalize_workdir_rel(value) == expected


@pytest.mark.parametrize(
    "value", ["/etc", "../fuera", "a/../../b", r"C:\Windows", "C:proj", r"\\server\share", "a/b:c", "a\x00b", pytest.param("x" * 501, id="demasiado-largo")]
)
def test_workdir_rejects_escapes(value):
    with pytest.raises(ValidationError):
        repo.normalize_workdir_rel(value)


def test_list_tasks_filters_and_orders(conn):
    a = repo.create_task(conn, "a")
    b = repo.create_task(conn, "b")
    repo.transition_task(conn, b.id, "queued")
    assert [t.id for t in repo.list_tasks(conn)] == [b.id, a.id]
    assert [t.id for t in repo.list_tasks(conn, statuses=["queued"])] == [b.id]
    assert repo.list_tasks(conn, statuses=[]) == []
    with pytest.raises(ValidationError):
        repo.list_tasks(conn, statuses=["inventado"])


def test_list_tasks_is_injection_safe(conn):
    repo.create_task(conn, "a")
    with pytest.raises(ValidationError):
        repo.list_tasks(conn, statuses=["pending') OR 1=1 --"])


def test_next_queued_by_priority_then_age(conn):
    low = repo.create_task(conn, "low", priority=0)
    high1 = repo.create_task(conn, "high1", priority=10)
    high2 = repo.create_task(conn, "high2", priority=10)
    pending = repo.create_task(conn, "pending", priority=50)
    for t in (low, high2, high1):
        repo.transition_task(conn, t.id, "queued")
    assert repo.next_queued_task(conn).id == high1.id
    assert pending.status == "pending"


def test_full_lifecycle_sets_finished_at(conn):
    t = repo.create_task(conn, "x")
    for status in ("queued", "running", "waiting_input", "queued", "running", "failed"):
        t = repo.transition_task(conn, t.id, status)
    assert t.finished_at is not None
    t = repo.transition_task(conn, t.id, "queued")  # reintento
    assert t.finished_at is None
    t = repo.transition_task(conn, repo.transition_task(conn, t.id, "running").id, "done")
    assert t.status == "done" and t.finished_at is not None


@pytest.mark.parametrize(("path", "bad"), [((), "running"), ((), "done"), (("queued", "running", "done"), "queued")])
def test_invalid_transitions(conn, path, bad):
    t = repo.create_task(conn, "x")
    for status in path:
        repo.transition_task(conn, t.id, status)
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(conn, t.id, bad)


def test_transition_expected_status(conn):
    t = repo.create_task(conn, "x")
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(conn, t.id, "queued", expected="waiting_input")
    assert repo.transition_task(conn, t.id, "queued", expected="pending").status == "queued"


def test_transition_unknown(conn):
    t = repo.create_task(conn, "x")
    with pytest.raises(ValidationError):
        repo.transition_task(conn, t.id, "volando")
    with pytest.raises(NotFoundError):
        repo.transition_task(conn, 9999, "queued")


def test_terminal_states_have_no_exits(conn):
    for terminal in ("done", "cancelled"):
        assert repo.TASK_TRANSITIONS[terminal] == frozenset()


def test_update_task(conn):
    t = repo.create_task(conn, "x", description="d")
    u = repo.update_task(conn, t.id, title="y", priority=3)
    assert (u.title, u.description, u.priority) == ("y", "d", 3)
    assert repo.update_task(conn, t.id) == u
    repo.transition_task(conn, t.id, "queued")
    repo.transition_task(conn, t.id, "running")
    with pytest.raises(InvalidTransitionError):
        repo.update_task(conn, t.id, title="z")


def test_delete_task_cascades(conn):
    t = repo.create_task(conn, "x")
    repo.ask_question(conn, t.id, "¿qué?")
    repo.transition_task(conn, t.id, "queued")
    repo.transition_task(conn, t.id, "running")
    run = repo.start_run(conn, t.id)
    conn.execute("INSERT INTO task_events (task_id, run_id, kind, message) VALUES (?, ?, 'progress', 'm')", (t.id, run.id))
    with pytest.raises(InvalidTransitionError):
        repo.delete_task(conn, t.id)
    repo.finish_run(conn, run.id, "ok")
    repo.transition_task(conn, t.id, "done")
    assert repo.delete_task(conn, t.id) is True
    assert repo.delete_task(conn, t.id) is False
    for table in ("questions", "runs", "task_events"):
        assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


# --- questions ----------------------------------------------------------------


def test_question_flow(conn):
    t = repo.create_task(conn, "x")
    q = repo.ask_question(conn, t.id, "¿Qué base uso?")
    assert q.status == "open" and q.answer is None
    q = repo.set_question_message(conn, q.id, 111, 555)
    assert repo.get_question_by_message(conn, 111, 555) == q
    assert repo.get_question_by_message(conn, 222, 555) is None  # mismo message_id, otro chat
    assert repo.list_open_questions(conn) == [q]
    assert repo.list_open_questions(conn, task_id=t.id) == [q]
    a = repo.answer_question(conn, q.id, "Postgres", answered_by=111)
    assert (a.status, a.answer, a.answered_by) == ("answered", "Postgres", 111)
    assert a.answered_at is not None
    assert repo.list_open_questions(conn) == []
    with pytest.raises(InvalidTransitionError):
        repo.answer_question(conn, q.id, "otra", answered_by=111)


def test_question_message_set_once_and_unique_per_chat(conn):
    t = repo.create_task(conn, "x")
    q1 = repo.ask_question(conn, t.id, "a")
    q2 = repo.ask_question(conn, t.id, "b")
    q3 = repo.ask_question(conn, t.id, "c")
    repo.set_question_message(conn, q1.id, 111, 10)
    with pytest.raises(InvalidTransitionError):
        repo.set_question_message(conn, q1.id, 111, 11)
    with pytest.raises(sqlite3.IntegrityError):
        repo.set_question_message(conn, q2.id, 111, 10)
    repo.set_question_message(conn, q3.id, 222, 10)  # otro chat: válido
    with pytest.raises(ValidationError):
        repo.set_question_message(conn, q2.id, 0, 12)


def test_expire_question(conn):
    t = repo.create_task(conn, "x")
    q = repo.ask_question(conn, t.id, "a")
    assert repo.expire_question(conn, q.id) is True
    assert repo.expire_question(conn, q.id) is False
    with pytest.raises(InvalidTransitionError):
        repo.answer_question(conn, q.id, "tarde", answered_by=111)


def test_question_validation(conn):
    t = repo.create_task(conn, "x")
    with pytest.raises(ValidationError):
        repo.ask_question(conn, t.id, "")
    with pytest.raises(NotFoundError):
        repo.ask_question(conn, 9999, "a")
    q = repo.ask_question(conn, t.id, "a")
    with pytest.raises(ValidationError):
        repo.answer_question(conn, q.id, "x" * 4001, answered_by=111)
    with pytest.raises(ValidationError):
        repo.answer_question(conn, q.id, "ok", answered_by=-1)
    repo.transition_task(conn, t.id, "cancelled")
    with pytest.raises(InvalidTransitionError):
        repo.ask_question(conn, t.id, "b")


# --- runs ---------------------------------------------------------------------


def running_task(conn):
    t = repo.create_task(conn, "x")
    repo.transition_task(conn, t.id, "queued")
    return repo.transition_task(conn, t.id, "running")


def test_run_flow(conn):
    t = running_task(conn)
    r = repo.start_run(conn, t.id)
    assert r.status == "running" and r.ended_at is None
    assert repo.list_active_runs(conn) == [r]
    r = repo.set_run_session(conn, r.id, "sess-123")
    assert r.session_id == "sess-123"
    r = repo.finish_run(conn, r.id, "ok", exit_code=0, summary="listo")
    assert (r.status, r.exit_code, r.summary) == ("ok", 0, "listo")
    assert r.ended_at is not None
    assert repo.list_active_runs(conn) == []
    assert repo.list_runs(conn, t.id) == [r]
    assert repo.get_run(conn, r.id) == r


def test_run_requires_running_task(conn):
    t = repo.create_task(conn, "x")
    with pytest.raises(InvalidTransitionError):
        repo.start_run(conn, t.id)


def test_only_one_active_run_per_task(conn):
    t = running_task(conn)
    repo.start_run(conn, t.id)
    with pytest.raises(InvalidTransitionError):
        repo.start_run(conn, t.id)


def test_finish_run_rules(conn):
    t = running_task(conn)
    r = repo.start_run(conn, t.id)
    with pytest.raises(ValidationError):
        repo.finish_run(conn, r.id, "running")
    repo.finish_run(conn, r.id, "timeout")
    with pytest.raises(InvalidTransitionError):
        repo.finish_run(conn, r.id, "ok")
    with pytest.raises(InvalidTransitionError):
        repo.set_run_session(conn, r.id, "tarde")
    with pytest.raises(NotFoundError):
        repo.finish_run(conn, 9999, "ok")


# --- task_events ----------------------------------------------------------------


def test_transitions_log_status_events(conn):
    t = repo.create_task(conn, "x")
    repo.transition_task(conn, t.id, "queued")
    repo.transition_task(conn, t.id, "running")
    events = repo.list_events(conn, t.id)
    assert [(e.kind, e.message) for e in events] == [
        ("status_change", "pending → queued"),
        ("status_change", "queued → running"),
    ]


def test_failed_transition_logs_nothing(conn):
    t = repo.create_task(conn, "x")
    with pytest.raises(InvalidTransitionError):
        repo.transition_task(conn, t.id, "done")
    assert repo.list_events(conn, t.id) == []


def test_add_and_list_events(conn):
    t = repo.create_task(conn, "x")
    repo.transition_task(conn, t.id, "queued")
    run_task = repo.transition_task(conn, t.id, "running")
    run = repo.start_run(conn, run_task.id)
    e1 = repo.add_event(conn, t.id, "progress", "  leyendo archivos  ", run_id=run.id)
    e2 = repo.add_event(conn, t.id, "error", "algo salió mal")
    assert (e1.kind, e1.message, e1.run_id, e1.notified) == ("progress", "leyendo archivos", run.id, 0)
    all_events = repo.list_events(conn, t.id)
    assert [e.id for e in all_events][-2:] == [e1.id, e2.id]
    assert repo.list_events(conn, t.id, after_id=e1.id) == [e2]
    assert len(repo.list_events(conn, t.id, limit=1)) == 1


def test_event_validation(conn):
    t = repo.create_task(conn, "x")
    other = repo.create_task(conn, "otra")
    repo.transition_task(conn, other.id, "queued")
    repo.transition_task(conn, other.id, "running")
    other_run = repo.start_run(conn, other.id)
    with pytest.raises(ValidationError):
        repo.add_event(conn, t.id, "inventado", "m")
    with pytest.raises(ValidationError):
        repo.add_event(conn, t.id, "progress", "   ")
    with pytest.raises(ValidationError):
        repo.add_event(conn, t.id, "progress", None)
    with pytest.raises(NotFoundError):
        repo.add_event(conn, 9999, "progress", "m")
    with pytest.raises(NotFoundError):
        repo.add_event(conn, t.id, "progress", "m", run_id=9999)
    with pytest.raises(ValidationError, match="no pertenece"):
        repo.add_event(conn, t.id, "progress", "m", run_id=other_run.id)


def test_long_event_message_is_truncated(conn):
    t = repo.create_task(conn, "x")
    e = repo.add_event(conn, t.id, "progress", "z" * 10_000)
    assert len(e.message) == repo.EVENT_MAX_LEN and e.message.endswith("…")


def test_only_done_failed_waiting_input_are_notified(conn):
    a = repo.create_task(conn, "a")
    for status in ("queued", "running", "waiting_input", "queued", "running", "failed", "queued", "running", "done"):
        repo.transition_task(conn, a.id, status)
    b = repo.create_task(conn, "b")
    repo.transition_task(conn, b.id, "cancelled")
    repo.add_event(conn, a.id, "error", "un error suelto")
    repo.add_event(conn, a.id, "progress", "avance")
    pending = repo.pending_notifications(conn)
    assert [p.message for p in pending] == ["running → waiting_input", "running → failed", "running → done"]
    assert all(p.task_id == a.id and p.notify == 1 for p in pending)
    # El resto queda en la base como historial.
    assert len(repo.list_events(conn, a.id)) == 11


def test_notifications_flow(conn):
    t = repo.create_task(conn, "x")
    repo.transition_task(conn, t.id, "queued")
    repo.transition_task(conn, t.id, "running")
    done = repo.transition_task(conn, t.id, "done")
    (pending,) = repo.pending_notifications(conn)
    progress = repo.add_event(conn, t.id, "progress", "p")
    assert repo.mark_notified(conn, [pending.id, pending.id]) == 1
    assert repo.mark_notified(conn, [pending.id]) == 0
    assert repo.mark_notified(conn, [progress.id]) == 0  # no notificable: no cambia
    assert repo.mark_notified(conn, []) == 0
    assert repo.pending_notifications(conn) == []
    assert done.status == "done"


def test_mark_notified_many_ids(conn):
    t = repo.create_task(conn, "x")
    conn.executemany(
        "INSERT INTO task_events (task_id, kind, message, notify) VALUES (?, 'status_change', 'm', 1)",
        [(t.id,)] * 1200,
    )
    ids = [e.id for e in repo.pending_notifications(conn, limit=1000)]
    ids += [e.id for e in repo.list_events(conn, t.id, after_id=ids[-1], limit=1000)]
    assert len(ids) == 1200
    assert repo.mark_notified(conn, ids) == 1200
    assert repo.pending_notifications(conn) == []


def test_migration_from_v1_keeps_data(app_paths):
    from coclody.db import migrations

    c = connect(app_paths.db_file)
    with transaction(c):
        for statement in migrations._split(migrations.MIGRATIONS[0]):
            c.execute(statement)
        c.execute("PRAGMA user_version = 1")
    c.execute("INSERT INTO tasks (title) VALUES ('vieja')")
    c.execute("INSERT INTO task_events (task_id, kind, message) VALUES (1, 'progress', 'antes')")
    assert migrate(c) == SCHEMA_VERSION
    (event,) = repo.list_events(c, 1)
    assert (event.message, event.notify, event.notified) == ("antes", 0, 0)
    assert repo.pending_notifications(c) == []
    c.close()


@pytest.mark.parametrize("bad", [[0], [-1], ["1"], [True], [1.0]])
def test_mark_notified_validates_ids(conn, bad):
    with pytest.raises(ValidationError):
        repo.mark_notified(conn, bad)


def test_events_are_deleted_with_task(conn):
    t = repo.create_task(conn, "x")
    repo.add_event(conn, t.id, "progress", "p")
    repo.transition_task(conn, t.id, "cancelled")
    repo.delete_task(conn, t.id)
    assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == 0


# --- notification_deliveries ---------------------------------------------------------------


def test_record_delivery_is_idempotent(conn):
    t = repo.create_task(conn, "x")
    e = repo.add_event(conn, t.id, "progress", "p")
    assert repo.record_delivery(conn, e.id, 111, 0, "sent", 55) is True
    assert repo.record_delivery(conn, e.id, 111, 0, "rejected") is False
    assert repo.record_delivery(conn, e.id, 222, 0, "rejected") is True
    assert repo.delivered(conn, e.id) == {(111, 0), (222, 0)}
    row = conn.execute("SELECT status, tg_message_id FROM notification_deliveries WHERE chat_id = 111").fetchone()
    assert tuple(row) == ("sent", 55)


@pytest.mark.parametrize(
    "args", [(0, 111, 0, "sent"), (1, 0, 0, "sent"), (1, 111, -1, "sent"), (1, 111, 0, "otro"), (1, 111, 0, "sent", 0)]
)
def test_record_delivery_validation(conn, args):
    t = repo.create_task(conn, "x")
    repo.add_event(conn, t.id, "progress", "p")
    with pytest.raises(ValidationError):
        repo.record_delivery(conn, *args)


def test_deliveries_cascade_with_task(conn):
    t = repo.create_task(conn, "x")
    e = repo.add_event(conn, t.id, "progress", "p")
    repo.record_delivery(conn, e.id, 111, 0, "sent", 1)
    repo.delete_task(conn, t.id)
    assert conn.execute("SELECT count(*) FROM notification_deliveries").fetchone()[0] == 0


def test_question_lookup_through_deliveries(conn):
    t = repo.create_task(conn, "x")
    q = repo.ask_question(conn, t.id, "¿a?")
    e = repo.add_event(conn, t.id, "question", "¿a?")
    repo.record_delivery(conn, e.id, 222, q.id, "sent", 77)
    repo.record_delivery(conn, e.id, 333, 0, "sent", 78)  # part 0 no es una pregunta
    assert repo.get_question_by_message(conn, 222, 77).id == q.id
    assert repo.get_question_by_message(conn, 333, 78) is None
    assert repo.get_question_by_message(conn, 111, 77) is None


# --- v4: tipo de pregunta -----------------------------------------------------------------------


def test_migration_v4_marks_existing_questions_as_open(app_paths):
    from coclody.db import migrations

    c = connect(app_paths.db_file)
    with transaction(c):
        for script in migrations.MIGRATIONS[:3]:
            for statement in migrations._split(script):
                c.execute(statement)
        c.execute("PRAGMA user_version = 3")
    c.execute("INSERT INTO tasks (title, status) VALUES ('vieja', 'waiting_input')")
    c.execute("INSERT INTO questions (task_id, question) VALUES (1, '¿antes?')")
    assert migrate(c) == SCHEMA_VERSION
    (q,) = repo.list_questions(c, 1)
    assert (q.question, q.kind) == ("¿antes?", "open")
    c.close()


def test_question_kind_is_validated(conn):
    t = repo.create_task(conn, "x")
    assert repo.ask_question(conn, t.id, "¿Sigo?", kind="yes_no").kind == "yes_no"
    with pytest.raises(ValidationError):
        repo.ask_question(conn, t.id, "¿Sigo?", kind="multiple")
    with pytest.raises(sqlite3.IntegrityError):  # la base también lo impide
        conn.execute("INSERT INTO questions (task_id, question, kind) VALUES (?, 'q', 'otra')", (t.id,))


def test_migration_v5_adds_task_prompts_to_a_v4_base(app_paths):
    from coclody.db import migrations

    c = connect(app_paths.db_file)
    with transaction(c):
        for script in migrations.MIGRATIONS[:4]:
            for statement in migrations._split(script):
                c.execute(statement)
        c.execute("PRAGMA user_version = 4")
    c.execute("INSERT INTO tasks (title) VALUES ('vieja')")
    assert migrate(c) == SCHEMA_VERSION
    repo.add_task_prompt(c, 1, 2)
    assert repo.take_task_prompt(c, 1, 2) and repo.get_task(c, 1).title == "vieja"
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO task_prompts (chat_id, message_id) VALUES (0, 1)")
    c.close()
