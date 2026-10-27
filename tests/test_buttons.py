"""Botones: teclado fijo, botones inline en tareas y Sí/No en preguntas, y el guard para callback_query.

Los callback_query se arman con la forma de la Bot API (fake_telegram.callback_update): el chat sale
del mensaje que tenía el botón y el usuario es quien lo tocó. Lo central es que un callback de un
chat no autorizado (o uno que el guard no puede evaluar) no llegue a la base: se verifica con un
espía sobre la conexión que usan los comandos, además del estado final.
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
from coclody.orchestrator.worker import QUESTION_MARKER, YES_NO_MARKER, CancelRegistry, Worker, parse_question
from coclody.security import ChatGuard, close_security_log, configure_security_log
from coclody.telegram_bot.app import Services, build_application
from coclody.telegram_bot.keyboards import BUTTON_HELP, BUTTON_LIST, BUTTON_NEW, parse_callback
from coclody.telegram_bot.notifier import flush_notifications
from tests.fake_telegram import (
    TOKEN,
    FakeTelegram,
    bot_message,
    bot_message_with_buttons,
    callback_update,
    inline_data,
    markup,
    user_text,
)

ME, BRO, STRANGER = 6956605022, 7000000001, 999


def claude(text: str) -> ClaudeResult:
    return ClaudeResult(status="ok", exit_code=0, session_id="123e4567-e89b-12d3-a456-426614174000",
                        result_text=text, summary=text, stdout_truncated=False, duration_s=0.1)


class Runner:
    """Claude falso: la primera ejecución devuelve ``first``; las siguientes terminan."""

    def __init__(self, first: str):
        self.first, self.prompts = first, []

    def __call__(self, inv, cancel):
        self.prompts.append(inv.stdin.decode())
        return claude(self.first if len(self.prompts) == 1 else "Listo.")


class SpyDb:
    """Envuelve la fábrica de conexiones de los comandos y cuenta cada apertura."""

    def __init__(self, factory):
        self.factory, self.opened = factory, 0

    def __call__(self):
        self.opened += 1
        return self.factory()


@pytest.fixture
def env(app_paths, conn, tmp_path):
    configure_security_log(app_paths.security_log)
    db = connection_factory(app_paths.db_file)
    ws = tmp_path / "ws"
    ws.mkdir()
    exe = tmp_path / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    exe.parent.mkdir()
    exe.write_bytes(b"")
    exe.chmod(0o755)
    cfg = ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=("Read(./**)",), timeout_minutes=1)
    runner = Runner(f"Necesito confirmar.\n{YES_NO_MARKER} ¿Borro los temporales?")
    worker = Worker(cfg, db, CancelRegistry(), runner)
    config = Config(TelegramConfig(frozenset({ME})), cfg, UpdatesConfig(False, None, 24))
    services = Services(config=config, db=db, worker=worker, guard=ChatGuard({ME}))
    spy = SpyDb(services.commands._db)
    services.commands._db = spy
    fake = FakeTelegram()
    app = build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())
    yield Env(services, db, conn, runner, spy, fake, app, app_paths.security_log)
    close_security_log()


class Env:
    def __init__(self, services, db, conn, runner, spy, fake, app, sec_log):
        self.services, self.db, self.conn, self.runner = services, db, conn, runner
        self.spy, self.fake, self.app, self.sec_log = spy, fake, app, sec_log

    async def process(self, update: dict) -> list[dict]:
        """Procesa un update y devuelve las llamadas a la API que provocó."""
        before = len(self.fake.calls)
        await self.app.process_update(Update.de_json(update, self.app.bot))
        return self.fake.calls[before:]

    def security_log(self) -> str:
        logging.getLogger("coclody.security").handlers[0].flush()
        return self.sec_log.read_text(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


def queued(conn, title="t", created_by=ME):
    t = repo.create_task(conn, title, created_by=created_by)
    return repo.transition_task(conn, t.id, "queued")


def texts(calls) -> list[str]:
    return [params["text"] for name, params in calls if name == "sendMessage"]


async def ask_yes_no(e: Env) -> tuple[dict, dict]:
    """Ejecuta una tarea hasta que hace una pregunta de sí/no y la envía. Devuelve (params, mensaje del bot)."""
    await e.app.initialize()
    queued(e.conn, "limpiar")
    assert (await asyncio.to_thread(e.services.worker.run_next)).status == "waiting_input"
    await flush_notifications(e.app.bot, e.db, {ME})
    (params,) = e.fake.sent()
    return params, bot_message_with_buttons(e.fake.returned_ids[-1], ME, params)


# --- guard: un callback no autorizado no toca la base ------------------------------------------------


@pytest.mark.parametrize(
    ("chat_id", "user_id", "chat_type", "reason"),
    [
        (STRANGER, STRANGER, "private", "not_in_whitelist"),
        (BRO, BRO, "private", "not_in_whitelist"),  # p. ej. un hermano que se sacó de la lista
        (ME, STRANGER, "private", "user_chat_mismatch"),
        (-100555, ME, "supergroup", "not_private_chat"),
    ],
)
def test_unauthorized_callback_never_reaches_the_db(env, chat_id, user_id, chat_type, reason):
    task = queued(env.conn)

    async def scenario():
        await env.app.initialize()
        message = bot_message(77, chat_id, f"Creé la tarea #{task.id}")
        message["chat"]["type"] = chat_type
        calls = []
        for data in (f"t:c:{task.id}", "q:1:s", f"t:r:{task.id}"):
            calls += await env.process(callback_update(chat_id, data, message=message, user_id=user_id))
        await env.app.shutdown()
        return calls

    assert run(scenario()) == []  # ni respuesta al botón ni mensaje
    assert env.spy.opened == 0
    assert repo.get_task(env.conn, task.id).status == "queued"
    log_text = env.security_log()
    assert f"reason={reason}" in log_text and "update=callback_query" in log_text


def test_callback_is_denied_when_the_guard_fails(env, monkeypatch, caplog):
    task = queued(env.conn)

    def broken(*args, **kwargs):
        raise RuntimeError("guard roto")

    monkeypatch.setattr(env.services.guard, "is_authorized", broken)

    async def scenario():
        await env.app.initialize()
        calls = await env.process(callback_update(ME, f"t:c:{task.id}", message=bot_message(77, ME, "x")))
        await env.app.shutdown()
        return calls

    with caplog.at_level(logging.ERROR):
        assert run(scenario()) == []
    assert env.spy.opened == 0 and repo.get_task(env.conn, task.id).status == "queued"
    assert "El guard falló" in caplog.text


@pytest.mark.parametrize("kind", ["inline_message", "game"])
def test_callback_without_message_or_data_is_ignored(env, kind):
    task = queued(env.conn)
    if kind == "inline_message":  # sin mensaje no hay chat: el guard lo rechaza por falta de IDs
        update = callback_update(ME, f"t:c:{task.id}", message=None, inline_message_id="AAQ")
    else:
        update = callback_update(ME, None, message=bot_message(77, ME, "x"))

    async def scenario():
        await env.app.initialize()
        calls = await env.process(update)
        await env.app.shutdown()
        return calls

    assert run(scenario()) == []
    assert env.spy.opened == 0 and repo.get_task(env.conn, task.id).status == "queued"


@pytest.mark.parametrize("data", ["t:c:0", "t:x:1", "t:c:1 ", "t:c:1\n", "q:1:si", "q:1:s:extra", "t:c:#1",
                                  "t:c:" + "9" * 17, "", "borrar todo"])
def test_malformed_callback_data_does_nothing(env, data):
    task = queued(env.conn)

    async def scenario():
        await env.app.initialize()
        calls = await env.process(callback_update(ME, data, message=bot_message(77, ME, "x")))
        await env.app.shutdown()
        return calls

    calls = run(scenario())
    assert [name for name, _ in calls] == ["answerCallbackQuery"]
    assert calls[0][1]["text"] == "Ese botón no es válido."
    assert env.spy.opened == 0 and repo.get_task(env.conn, task.id).status == "queued"


def test_parse_callback_is_strict():
    assert parse_callback("t:c:12").action == "cancel" and parse_callback("t:r:12").task_id == 12
    assert parse_callback("q:3:n").answer == "no" and parse_callback("q:3:s").answer == "sí"
    assert parse_callback(None) is None and parse_callback(b"t:c:1") is None and parse_callback("T:C:1") is None


# --- botones inline en tareas ----------------------------------------------------------------------------


def test_new_task_shows_cancel_button_and_it_works(env):
    async def scenario():
        await env.app.initialize()
        calls = await env.process(user_text(ME, "/nueva Ordenar fotos"))
        (_, params), = [c for c in calls if c[0] == "sendMessage"]
        assert inline_data(params) == ["t:c:1"]
        message = bot_message_with_buttons(env.fake.returned_ids[-1], ME, params)
        pressed = await env.process(callback_update(ME, "t:c:1", message=message))
        again = await env.process(callback_update(ME, "t:c:1", message=message))
        await env.app.shutdown()
        return pressed, again

    pressed, again = run(scenario())
    assert [name for name, _ in pressed] == ["answerCallbackQuery", "sendMessage"]
    assert texts(pressed) == ["Cancelé la tarea #1."]
    assert texts(again) == ["La tarea #1 ya terminó (cancelada)."]
    assert repo.get_task(env.conn, 1).status == "cancelled"


def test_failed_notification_has_retry_button(env):
    env.runner.first = "x"
    t = queued(env.conn, "migrar")
    repo.transition_task(env.conn, t.id, "running")
    r = repo.start_run(env.conn, t.id)
    repo.finish_run(env.conn, r.id, "error", summary="se rompió")
    repo.transition_task(env.conn, t.id, "failed")

    async def scenario():
        await env.app.initialize()
        await flush_notifications(env.app.bot, env.db, {ME})
        (params,) = env.fake.sent()
        assert inline_data(params) == [f"t:r:{t.id}"]
        message = bot_message_with_buttons(env.fake.returned_ids[-1], ME, params)
        calls = await env.process(callback_update(ME, f"t:r:{t.id}", message=message))
        await env.app.shutdown()
        return calls

    assert texts(run(scenario())) == [f"La tarea #{t.id} volvió a la cola."]
    assert repo.get_task(env.conn, t.id).status == "queued"


def test_list_and_status_offer_buttons_per_task(env):
    a = queued(env.conn, "activa")
    b = queued(env.conn, "fallida")
    repo.transition_task(env.conn, b.id, "running")
    repo.transition_task(env.conn, b.id, "failed")

    async def scenario():
        await env.app.initialize()
        listing = await env.process(user_text(ME, "/lista"))
        status_a = await env.process(user_text(ME, f"/estado {a.id}"))
        status_missing = await env.process(user_text(ME, "/estado 99"))
        await env.app.shutdown()
        return [p for n, p in listing + status_a + status_missing if n == "sendMessage"]

    listing, status_a, status_missing = run(scenario())
    assert inline_data(listing) == [f"t:c:{a.id}", f"t:r:{b.id}"]
    texts_ = [b["text"] for row in markup(listing)["inline_keyboard"] for b in row]
    assert texts_ == [f"✖ Cancelar #{a.id}", f"↻ Reintentar #{b.id}"]
    assert inline_data(status_a) == [f"t:c:{a.id}"]
    assert markup(status_missing) is None


# --- Sí/No en preguntas --------------------------------------------------------------------------------


def test_yes_no_question_end_to_end(env):
    async def scenario():
        params, message = await ask_yes_no(env)
        assert inline_data(params) == ["q:1:s", "q:1:n"]
        assert "Tocá Sí o No" in params["text"]
        pressed = await env.process(callback_update(ME, "q:1:s", message=message))
        again = await env.process(callback_update(ME, "q:1:n", message=message))
        await env.app.shutdown()
        return pressed, again

    pressed, again = run(scenario())
    assert [name for name, _ in pressed] == ["answerCallbackQuery", "editMessageReplyMarkup", "sendMessage"]
    assert texts(pressed) == ["Respuesta: sí. Respuesta registrada. La tarea #1 vuelve a la cola."]
    assert texts(again) == ["Esa pregunta ya no está pendiente."]
    (q,) = repo.list_questions(env.conn, 1)
    assert (q.kind, q.status, q.answer, q.answered_by) == ("yes_no", "answered", "sí", ME)

    final = env.services.worker.run_next()
    assert final.status == "done" and "Respuesta: sí" in env.runner.prompts[1]


def test_yes_no_question_still_accepts_a_text_reply(env):
    async def scenario():
        _, message = await ask_yes_no(env)
        calls = await env.process(user_text(ME, "sí, pero solo los de hoy", reply_to=message))
        await env.app.shutdown()
        return calls

    assert texts(run(scenario())) == ["Respuesta registrada. La tarea #1 vuelve a la cola."]
    assert repo.list_questions(env.conn, 1)[0].answer == "sí, pero solo los de hoy"


def test_yes_no_button_only_counts_on_its_own_message(env):
    async def scenario():
        _, message = await ask_yes_no(env)
        elsewhere = bot_message(message["message_id"] + 50, ME, "otro mensaje")
        forged = await env.process(callback_update(ME, "q:1:s", message=elsewhere))
        wrong_question = await env.process(callback_update(ME, "q:2:s", message=message))
        await env.app.shutdown()
        return forged + wrong_question

    assert texts(run(scenario())) == ["Ese botón no corresponde a una pregunta pendiente."] * 2
    assert repo.list_questions(env.conn, 1)[0].status == "open"


def test_open_question_has_no_yes_no_buttons(env):
    env.runner.first = f"Necesito un dato.\n{QUESTION_MARKER} ¿Qué carpeta?"

    async def scenario():
        params, _ = await ask_yes_no(env)
        await env.app.shutdown()
        return params

    params = run(scenario())
    assert markup(params) is None and "Tocá Sí o No" not in params["text"]
    assert repo.list_questions(env.conn, 1)[0].kind == "open"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (f"{YES_NO_MARKER} ¿Sigo?", ("¿Sigo?", "yes_no")),
        (f"{QUESTION_MARKER} ¿Cuál?", ("¿Cuál?", "open")),
        (f"{YES_NO_MARKER}   ", None),
        ("COCLODY_PREGUNTA_SINO ¿sin dos puntos?", None),
    ],
)
def test_parse_question_kinds(text, expected):
    assert parse_question(text) == expected


def test_prompt_explains_yes_no_marker(env):
    async def scenario():
        await ask_yes_no(env)
        await env.app.shutdown()

    run(scenario())
    assert YES_NO_MARKER in env.runner.prompts[0] and QUESTION_MARKER in env.runner.prompts[0]


# --- teclado fijo --------------------------------------------------------------------------------------


def test_start_sets_the_main_keyboard(env):
    async def scenario():
        await env.app.initialize()
        calls = await env.process(user_text(ME, "/start"))
        await env.app.shutdown()
        return calls

    (params,) = [p for n, p in run(scenario()) if n == "sendMessage"]
    keyboard = markup(params)
    assert [b if isinstance(b, str) else b["text"] for row in keyboard["keyboard"] for b in row] == [
        BUTTON_NEW, BUTTON_LIST, BUTTON_HELP]
    assert keyboard["is_persistent"] and keyboard["resize_keyboard"]


def test_keyboard_list_and_help(env):
    queued(env.conn, "una")

    async def scenario():
        await env.app.initialize()
        listing = await env.process(user_text(ME, BUTTON_LIST))
        help_ = await env.process(user_text(ME, BUTTON_HELP))
        await env.app.shutdown()
        return texts(listing), texts(help_)

    listing, help_ = run(scenario())
    assert "#1 [en cola] una" in listing[0]
    assert "/nueva" in help_[0]


def test_new_task_button_asks_for_title_and_reply_creates_it(env):
    async def scenario():
        await env.app.initialize()
        calls = await env.process(user_text(ME, BUTTON_NEW))
        (params,) = [p for n, p in calls if n == "sendMessage"]
        assert markup(params)["force_reply"] is True
        prompt = bot_message(env.fake.returned_ids[-1], ME, params["text"])
        created = await env.process(user_text(ME, "Ordenar fotos\nPor año", reply_to=prompt, message_id=6001))
        twice = await env.process(user_text(ME, "Otra", reply_to=prompt, message_id=6002))
        loose = await env.process(user_text(ME, "Título suelto", message_id=6003))
        await env.app.shutdown()
        return texts(created), texts(twice), texts(loose)

    created, twice, loose = run(scenario())
    assert created[0].startswith("Creé la tarea #1: Ordenar fotos")
    assert "ni un pedido de título vigente" in twice[0]  # el pedido se usa una sola vez
    assert loose == ["No entendí. Mandá /ayuda para ver los comandos."]  # sin reply no se adivina
    (task,) = repo.list_tasks(env.conn)
    assert (task.title, task.description, task.created_by) == ("Ordenar fotos", "Por año", ME)


def test_title_prompt_belongs_to_the_chat_that_asked(env):
    env.services.guard = ChatGuard({ME, BRO})

    async def scenario():
        await env.app.initialize()
        calls = await env.process(user_text(ME, BUTTON_NEW))
        (params,) = [p for n, p in calls if n == "sendMessage"]
        prompt_id = env.fake.returned_ids[-1]
        # BRO no puede crear tareas respondiendo a un mensaje con el mismo id en su chat.
        other = await env.process(user_text(BRO, "Intruso", reply_to=bot_message(prompt_id, BRO, params["text"])))
        await env.app.shutdown()
        return texts(other)

    assert "ni un pedido de título vigente" in run(scenario())[0]
    assert repo.list_tasks(env.conn) == []


def test_keyboard_label_sent_as_reply_to_a_question_is_not_an_answer(env):
    async def scenario():
        _, message = await ask_yes_no(env)
        calls = await env.process(user_text(ME, BUTTON_LIST, reply_to=message))
        await env.app.shutdown()
        return texts(calls)

    (reply,) = run(scenario())
    assert "#1 [esperando respuesta] limpiar" in reply
    assert repo.list_questions(env.conn, 1)[0].status == "open"
