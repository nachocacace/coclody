from __future__ import annotations

import asyncio
import logging
import sys

import pytest
from telegram import Bot, Update

from coclody.config import ClaudeConfig, Config, TelegramConfig, UpdatesConfig
from coclody.db import connection_factory, repo
from coclody.orchestrator.worker import CancelRegistry, Worker
from coclody.security import ChatGuard, close_security_log, configure_security_log
from coclody.telegram_bot.app import Services, build_application
from coclody.telegram_bot.commands import Commands, parse_task_id
from coclody.telegram_bot.notifier import flush_notifications
from tests.fake_telegram import TOKEN, FakeTelegram, message_update

ME, BRO, STRANGER = 111, 222, 999


@pytest.fixture
def db(conn, app_paths):
    return connection_factory(app_paths.db_file)


@pytest.fixture
def cancels():
    return CancelRegistry()


@pytest.fixture
def cmds(db, cancels):
    return Commands(db, cancels)


@pytest.fixture
def sec_log(app_paths):
    configure_security_log(app_paths.security_log)
    yield app_paths.security_log
    close_security_log()


@pytest.fixture
def services(db, cancels, tmp_path, sec_log):
    ws = tmp_path / "ws"
    ws.mkdir()
    exe = tmp_path / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    exe.parent.mkdir()
    exe.write_bytes(b"")
    exe.chmod(0o755)
    claude = ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=("Read",), timeout_minutes=1)
    cfg = Config(
        telegram=TelegramConfig(allowed_chat_ids=frozenset({ME, BRO})),
        claude=claude,
        updates=UpdatesConfig(enabled=False, github_repo=None, check_interval_hours=24),
    )
    return Services(config=cfg, db=db, worker=Worker(claude, db, cancels), guard=ChatGuard({ME, BRO}))


class Harness:
    def __init__(self, services):
        self.fake = FakeTelegram()
        self.app = build_application(TOKEN, services, request=self.fake, get_updates_request=FakeTelegram())

    async def __aenter__(self):
        await self.app.initialize()
        return self

    async def __aexit__(self, *exc):
        await self.app.shutdown()

    async def send(self, chat_id, text, **kw) -> list[str]:
        before = len(self.fake.sent())
        await self.app.process_update(Update.de_json(message_update(chat_id, text, **kw), self.app.bot))
        return [m["text"] for m in self.fake.sent()[before:]]


def run(coro):
    return asyncio.run(coro)


def queued_task(conn, title="t", created_by=ME):
    t = repo.create_task(conn, title, created_by=created_by)
    return repo.transition_task(conn, t.id, "queued")


def to_waiting(conn, task, question="¿Qué color?"):
    repo.transition_task(conn, task.id, "running")
    repo.ask_question(conn, task.id, question)
    return repo.transition_task(conn, task.id, "waiting_input")


# --- comandos (lógica pura) ------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [("5", 5), ("#12", 12), (" 7 ", 7), ("0", None), ("-1", None),
                                               ("1e3", None), ("5 6", None), ("", None), ("9" * 17, None)])
def test_parse_task_id(raw, expected):
    assert parse_task_id(raw) == expected


def test_new_task_and_list(cmds, conn):
    reply = cmds.new_task(ME, "Arreglar login\nEl botón no responde\nen Safari")
    assert "Creé la tarea #1" in reply and "cola" in reply
    t = repo.get_task(conn, 1)
    assert (t.title, t.description, t.status, t.created_by) == ("Arreglar login", "El botón no responde\nen Safari", "queued", ME)
    assert "#1 [en cola] Arreglar login" in cmds.list_tasks()


def test_new_task_validation(cmds, conn):
    assert cmds.new_task(ME, "").startswith("Uso:")
    assert "No pude crear" in cmds.new_task(ME, "x" * 300)
    assert repo.list_tasks(conn) == []


def test_list_empty_and_finished(cmds, conn):
    assert "No hay tareas" in cmds.list_tasks()
    t = repo.create_task(conn, "vieja")
    repo.transition_task(conn, t.id, "cancelled")
    text = cmds.list_tasks()
    assert "No hay tareas activas" in text and "#1 [cancelada] vieja" in text


def test_status(cmds, conn):
    t = to_waiting(conn, queued_task(conn, "migrar"))
    text = cmds.status(str(t.id))
    assert "#1 migrar" in text and "esperando respuesta" in text and "¿Qué color?" in text
    assert "running → waiting_input" in text
    assert cmds.status("99") == "No existe la tarea #99."
    assert cmds.status("abc").startswith("Uso:")


def test_answer_command_requeues(cmds, conn):
    t = to_waiting(conn, queued_task(conn))
    assert cmds.answer(ME, f"{t.id}\nazul") == f"Respuesta registrada. La tarea #{t.id} vuelve a la cola."
    assert repo.get_task(conn, t.id).status == "queued"
    q = repo.list_questions(conn, t.id)[0]
    assert (q.status, q.answer, q.answered_by) == ("answered", "azul", ME)
    assert "no tiene preguntas pendientes" in cmds.answer(ME, f"{t.id} otra")


def test_answer_with_several_questions(cmds, conn):
    t = to_waiting(conn, queued_task(conn))
    repo.ask_question(conn, t.id, "¿Y el tamaño?")
    assert "1 pregunta(s) más" in cmds.answer(ME, f"{t.id} azul")
    assert repo.get_task(conn, t.id).status == "waiting_input"
    assert "vuelve a la cola" in cmds.answer(ME, f"{t.id} grande")


@pytest.mark.parametrize("text", ["", "5", "abc azul", "#0 azul"])
def test_answer_usage(cmds, text):
    assert cmds.answer(ME, text).startswith("Uso:")


def test_answer_reply(cmds, conn):
    t = to_waiting(conn, queued_task(conn))
    q = repo.list_open_questions(conn, task_id=t.id)[0]
    repo.set_question_message(conn, q.id, ME, 4242)
    assert cmds.answer_reply(BRO, 4242, "rojo") is None  # mismo id de mensaje en otro chat
    assert cmds.answer_reply(ME, 1, "rojo") is None
    assert "vuelve a la cola" in cmds.answer_reply(ME, 4242, "rojo")
    assert cmds.answer_reply(ME, 4242, "otra vez") == "Esa pregunta ya no está pendiente."


def test_cancel_queued_and_waiting(cmds, conn):
    a = queued_task(conn)
    assert cmds.cancel(str(a.id)) == f"Cancelé la tarea #{a.id}."
    b = to_waiting(conn, queued_task(conn))
    assert cmds.cancel(f"#{b.id}") == f"Cancelé la tarea #{b.id}."
    assert repo.list_open_questions(conn, task_id=b.id) == []
    assert "ya terminó (cancelada)" in cmds.cancel(str(a.id))
    assert cmds.cancel("77") == "No existe la tarea #77."


def test_cancel_running_signals_worker(cmds, conn, cancels):
    t = queued_task(conn)
    event = cancels.register(t.id)
    repo.transition_task(conn, t.id, "running")
    assert cmds.cancel(str(t.id)) == f"Cancelando la tarea #{t.id}…"
    assert event.is_set() and cancels.reason(t.id) == "user"
    assert repo.get_task(conn, t.id).status == "running"  # el worker la cierra


def test_cancel_running_not_registered(cmds, conn):
    t = queued_task(conn)
    repo.transition_task(conn, t.id, "running")
    assert "está terminando" in cmds.cancel(str(t.id))


# --- aplicación de Telegram con cliente simulado ------------------------------------------


def test_bot_end_to_end_commands(services, conn):
    async def scenario():
        async with Harness(services) as h:
            assert "/nueva" in (await h.send(ME, "/ayuda"))[0]
            (reply,) = await h.send(ME, "/nueva Preparar informe\nCon gráficos")
            assert "Creé la tarea #1" in reply
            (reply,) = await h.send(ME, "/lista")
            assert "#1 [en cola] Preparar informe" in reply
            (reply,) = await h.send(BRO, "/estado@coclody_test_bot 1")
            assert "Preparar informe" in reply
            (reply,) = await h.send(ME, "/cancelar 1")
            assert reply == "Cancelé la tarea #1."
            (reply,) = await h.send(ME, "hola")
            assert "/ayuda" in reply
            (reply,) = await h.send(ME, "/inventado")
            assert "/ayuda" in reply
            return h.fake.sent()

    sent = run(scenario())
    assert all(m["chat_id"] in (ME, BRO) for m in sent)
    assert all("parse_mode" not in m for m in sent)  # siempre texto plano
    assert repo.get_task(conn, 1).created_by == ME


@pytest.mark.parametrize(
    ("chat_id", "user_id", "chat_type", "reason"),
    [
        (STRANGER, STRANGER, "private", "not_in_whitelist"),
        (ME, STRANGER, "private", "user_chat_mismatch"),
        (-100555, ME, "supergroup", "not_private_chat"),
        (ME, ME, "group", "not_private_chat"),
    ],
)
def test_bot_rejects_unauthorized_and_logs(services, conn, sec_log, chat_id, user_id, chat_type, reason):
    async def scenario():
        async with Harness(services) as h:
            replies = await h.send(chat_id, "/nueva borrar todo", user_id=user_id, chat_type=chat_type)
            return replies

    assert run(scenario()) == []  # ni siquiera una respuesta
    assert repo.list_tasks(conn) == []
    logging.getLogger("coclody.security").handlers[0].flush()
    log_text = sec_log.read_text(encoding="utf-8")
    assert f"reason={reason}" in log_text and "borrar todo" not in log_text


def test_bot_reply_to_question_message(services, conn, db):
    t = to_waiting(conn, queued_task(conn, "pintar"))

    async def scenario():
        async with Harness(services) as h:
            await flush_notifications(h.app.bot, db, {ME, BRO})
            (question_msg,) = h.fake.sent()
            assert question_msg["chat_id"] == ME  # solo a quien creó la tarea
            assert "¿Qué color?" in question_msg["text"]
            q = repo.list_questions(conn, t.id)[0]
            assert q.tg_chat_id == ME
            (reply,) = await h.send(ME, "verde", reply_to=q.tg_message_id)
            assert "vuelve a la cola" in reply
            (reply,) = await h.send(ME, "otra cosa", reply_to=5)
            assert "no es una pregunta pendiente" in reply

    run(scenario())
    assert repo.get_task(conn, t.id).status == "queued"
    assert repo.list_questions(conn, t.id)[0].answer == "verde"


def test_bot_internal_error_is_reported_without_details(services, conn, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("detalle interno con " + TOKEN)

    monkeypatch.setattr(services.commands, "list_tasks", boom)

    async def scenario():
        async with Harness(services) as h:
            return await h.send(ME, "/lista")

    (reply,) = run(scenario())
    assert reply == "Ocurrió un error interno. Quedó registrado en el log."


# --- notificaciones ------------------------------------------------------------------


def finish(conn, task, status, summary):
    if task.status == "queued":
        repo.transition_task(conn, task.id, "running")
    run_ = repo.start_run(conn, task.id)
    repo.finish_run(conn, run_.id, "ok" if status == "done" else "error", summary=summary)
    return repo.transition_task(conn, task.id, status)


def notify(db, fake, allowed=(ME, BRO)):
    async def go():
        async with Bot(TOKEN, request=fake) as bot:
            return await flush_notifications(bot, db, set(allowed))

    return run(go())


def test_notifies_done_and_failed_with_summary(conn, db):
    a = finish(conn, queued_task(conn, "uno"), "done", "Hice uno.")
    b = finish(conn, queued_task(conn, "dos", created_by=BRO), "failed", "Se rompió dos.")
    fake = FakeTelegram()
    assert notify(db, fake) == 2
    first, second = fake.sent()
    assert first["chat_id"] == ME and "✅ Tarea #1 terminada: uno" in first["text"] and "Hice uno." in first["text"]
    assert second["chat_id"] == BRO and "❌ Tarea #2 falló: dos" in second["text"] and "Se rompió dos." in second["text"]
    assert repo.pending_notifications(conn) == []
    assert notify(db, FakeTelegram()) == 0
    assert a.status == "done" and b.status == "failed"


def test_other_events_are_not_sent(conn, db):
    t = queued_task(conn)
    repo.add_event(conn, t.id, "progress", "avance")
    repo.transition_task(conn, t.id, "cancelled")
    fake = FakeTelegram()
    assert notify(db, fake) == 0 and fake.sent() == []


def test_task_without_valid_creator_goes_to_whole_whitelist(conn, db):
    finish(conn, queued_task(conn, created_by=None), "done", "ok")
    finish(conn, queued_task(conn, created_by=STRANGER), "done", "ok")  # creador ya no autorizado
    fake = FakeTelegram()
    notify(db, fake)
    assert sorted(m["chat_id"] for m in fake.sent()) == [ME, ME, BRO, BRO]
    assert STRANGER not in [m["chat_id"] for m in fake.sent()]


def test_transient_error_keeps_event_for_retry(conn, db):
    finish(conn, queued_task(conn), "done", "ok")
    fake = FakeTelegram()
    fake.fail_next("sendMessage", 502, "Bad Gateway")
    assert notify(db, fake) == 0
    assert len(repo.pending_notifications(conn)) == 1
    retry = FakeTelegram()
    assert notify(db, retry) == 1 and len(retry.sent()) == 1


def test_permanent_error_is_not_retried_forever(conn, db, caplog):
    finish(conn, queued_task(conn), "done", "ok")
    fake = FakeTelegram()
    fake.fail_next("sendMessage", 403, "Forbidden: bot was blocked by the user")
    with caplog.at_level(logging.WARNING):
        assert notify(db, fake) == 1
    assert repo.pending_notifications(conn) == []
    assert "rechazó" in caplog.text


def test_no_whitelist_sends_nothing(conn, db):
    finish(conn, queued_task(conn), "done", "ok")
    fake = FakeTelegram()
    assert notify(db, fake, allowed=()) == 0 and fake.sent() == []


def test_long_summary_is_clipped(conn, db):
    finish(conn, queued_task(conn), "done", "z" * 9000)
    fake = FakeTelegram()
    notify(db, fake)
    assert len(fake.sent()[0]["text"]) <= 4096
