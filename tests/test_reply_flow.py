"""Flujo completo de una pregunta: Claude pregunta, el bot la envía, el usuario responde y la
respuesta llega a la tarea correcta.

El test anterior (test_bot_reply_to_question_message) armaba el reply con exactamente la forma que
el código esperaba, así que no podía fallar por la forma del update. En producción, un mensaje que
Telegram no marcó como reply (sin ``reply_to_message``) terminó en "No entendí". Acá los updates se
arman con la forma de la Bot API (fake_telegram.bot_message / user_text) e incluyen las variantes
reales: reply normal, reply a otro hilo (``external_reply``) y mensaje sin reply (p. ej. contestado
desde la notificación).
"""

from __future__ import annotations

import asyncio
import logging
import sys

import pytest
from telegram import Update

from coclody.config import ClaudeConfig, Config, TelegramConfig, UpdatesConfig
from coclody.db import connection_factory, repo
from coclody.orchestrator.claude_runner import ClaudeResult
from coclody.orchestrator.worker import QUESTION_MARKER, CancelRegistry, Worker
from coclody.security import ChatGuard, close_security_log, configure_security_log
from coclody.telegram_bot.app import Services, build_application
from coclody.telegram_bot.notifier import flush_notifications
from tests.fake_telegram import TOKEN, FakeTelegram, bot_message, external_reply_to, user_text

ME = 6956605022
OTHER_CHAT = 123


def claude(text: str) -> ClaudeResult:
    return ClaudeResult(status="ok", exit_code=0, session_id="123e4567-e89b-12d3-a456-426614174000",
                        result_text=text, summary=text, stdout_truncated=False, duration_s=0.1)


class ScriptedRunner:
    """Claude falso: pregunta en la primera ejecución de cada tarea y termina en la segunda."""

    def __init__(self):
        self.prompts: dict[str, list[str]] = {}

    def __call__(self, inv, cancel):
        prompt = inv.stdin.decode()
        title = "A" if "Tarea A" in prompt else "B"
        self.prompts.setdefault(title, []).append(prompt)
        if len(self.prompts[title]) == 1:
            return claude(f"Necesito un dato.\n{QUESTION_MARKER} ¿Qué color para la tarea {title}?")
        return claude(f"Listo {title}.")


@pytest.fixture
def setup(app_paths, conn, tmp_path):
    configure_security_log(app_paths.security_log)
    db = connection_factory(app_paths.db_file)
    ws = tmp_path / "ws"
    ws.mkdir()
    exe = tmp_path / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    exe.parent.mkdir()
    exe.write_bytes(b"")
    exe.chmod(0o755)
    cfg = ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=("Read(./**)",), timeout_minutes=1)
    runner = ScriptedRunner()
    worker = Worker(cfg, db, CancelRegistry(), runner)
    config = Config(TelegramConfig(frozenset({ME})), cfg, UpdatesConfig(False, None, 24))
    services = Services(config=config, db=db, worker=worker, guard=ChatGuard({ME}))
    yield services, db, runner
    close_security_log()


def run(coro):
    return asyncio.run(coro)


async def ask_both(services, db):
    """Crea A y B, las ejecuta hasta que preguntan y envía las dos preguntas. Devuelve el bot y los mensajes."""
    fake = FakeTelegram()
    app = build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())
    await app.initialize()
    with db() as conn:
        for title in ("Tarea A", "Tarea B"):
            t = repo.create_task(conn, title, created_by=ME)
            repo.transition_task(conn, t.id, "queued")
    assert (await asyncio.to_thread(services.worker.run_next)).status == "waiting_input"
    assert (await asyncio.to_thread(services.worker.run_next)).status == "waiting_input"
    await flush_notifications(app.bot, db, {ME})
    # Cada pregunta, como la verá el usuario: el mensaje del bot con su message_id real y sus entidades.
    sent = {}
    for params, message_id in zip(fake.sent(), fake.returned_ids):
        task_id = int(params["text"].split("#", 1)[1].split(" ", 1)[0])
        sent[task_id] = bot_message(message_id, ME, params["text"])
    return app, fake, sent


async def deliver(app, fake, update: dict) -> list[str]:
    before = len(fake.sent())
    await app.process_update(Update.de_json(update, app.bot))
    return [m["text"] for m in fake.sent()[before:]]


def state(db):
    with db() as conn:
        return {t.title: (t.status, [(q.status, q.answer) for q in repo.list_questions(conn, t.id)])
                for t in repo.list_tasks(conn)}


def test_reply_to_question_reaches_the_right_task(setup):
    services, db, runner = setup

    async def scenario():
        app, fake, sent = await ask_both(services, db)
        replies = await deliver(app, fake, user_text(ME, "azul", reply_to=sent[2]))  # responde la de B
        await app.shutdown()
        return replies

    assert run(scenario()) == ["Respuesta registrada. La tarea #2 vuelve a la cola."]
    s = state(db)
    assert s["Tarea B"] == ("queued", [("answered", "azul")])
    assert s["Tarea A"] == ("waiting_input", [("open", None)])  # la otra tarea no se tocó

    final = services.worker.run_next()  # B sigue con la respuesta en el prompt
    assert (final.title, final.status) == ("Tarea B", "done")
    assert "Respuesta: azul" in runner.prompts["B"][1]
    assert "B" in runner.prompts and len(runner.prompts["A"]) == 1


def test_reply_quoting_part_of_the_question(setup):
    services, db, _ = setup

    async def scenario():
        app, fake, sent = await ask_both(services, db)
        replies = await deliver(app, fake, user_text(ME, "rojo", reply_to=sent[1], quote="¿Qué color"))
        await app.shutdown()
        return replies

    assert "vuelve a la cola" in run(scenario())[0]
    assert state(db)["Tarea A"] == ("queued", [("answered", "rojo")])


def test_reply_from_another_thread_arrives_as_external_reply(setup):
    services, db, _ = setup

    async def scenario():
        app, fake, sent = await ask_both(services, db)
        replies = await deliver(app, fake, user_text(ME, "verde", external_reply=external_reply_to(sent[2]),
                                                     thread_id=77))
        await app.shutdown()
        return replies

    assert "vuelve a la cola" in run(scenario())[0]
    assert state(db)["Tarea B"] == ("queued", [("answered", "verde")])


def test_external_reply_to_another_chat_is_never_an_answer(setup):
    services, db, _ = setup

    async def scenario():
        app, fake, sent = await ask_both(services, db)
        foreign = bot_message(sent[2]["message_id"], OTHER_CHAT, sent[2]["text"])  # mismo id, otro chat
        replies = await deliver(app, fake, user_text(ME, "verde", external_reply=external_reply_to(foreign)))
        await app.shutdown()
        return replies

    (reply,) = run(scenario())
    assert "no llegó como respuesta" in reply
    assert all(q[0] == "open" for _, qs in state(db).values() for q in qs)


def test_message_without_reply_metadata_is_not_guessed(setup, caplog):
    """Lo que pasó en producción: texto sin reply_to_message (p. ej. desde la notificación)."""
    services, db, _ = setup

    async def scenario():
        app, fake, _ = await ask_both(services, db)
        replies = await deliver(app, fake, user_text(ME, "No, no te doy permiso"))
        await app.shutdown()
        return replies

    with caplog.at_level(logging.INFO):
        (reply,) = run(scenario())
    assert "No entendí" not in reply
    assert "no llegó como respuesta" in reply and "#1, #2" in reply and "/responder 2" in reply
    assert all(q[0] == "open" for _, qs in state(db).values() for q in qs)  # no se asignó a ciegas
    assert "reply_to=False external_reply=False" in caplog.text
    assert "No, no te doy permiso" not in caplog.text  # se registra la forma, nunca el texto


def test_reply_to_a_bot_message_that_is_not_a_question(setup):
    services, db, _ = setup

    async def scenario():
        app, fake, _ = await ask_both(services, db)
        other = bot_message(4242, ME, "Creé la tarea #1: Tarea A")
        replies = await deliver(app, fake, user_text(ME, "hola", reply_to=other))
        await app.shutdown()
        return replies

    assert run(scenario()) == ["Ese mensaje no es una pregunta pendiente. Usá /responder <id> <texto>."]


def test_plain_text_without_pending_questions_is_still_no_entendi(setup):
    services, db, _ = setup

    async def scenario():
        fake = FakeTelegram()
        app = build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())
        await app.initialize()
        replies = await deliver(app, fake, user_text(ME, "hola"))
        await app.shutdown()
        return replies

    assert run(scenario()) == ["No entendí. Mandá /ayuda para ver los comandos."]


def test_question_message_explains_how_to_answer(setup):
    services, db, _ = setup

    async def scenario():
        app, fake, sent = await ask_both(services, db)
        await app.shutdown()
        return sent

    text = run(scenario())[1]["text"]
    assert "mantené apretado este mensaje → Responder" in text and "/responder 1" in text
    assert "notificación" in text
