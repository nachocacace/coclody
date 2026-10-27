"""Interfaz en Telegram: menú de comandos desde el arranque, teclado fijo en cada mensaje, emojis
de estado y títulos sin autolinks.

Que Telegram convierta "README.md" en link pasa del lado de Telegram, no en la Bot API: acá se
verifica lo que mandamos (el título sin ningún punto, arroba, numeral, pesos, barra o dos puntos
seguido directamente de texto), no cómo lo dibuja el cliente.
"""

from __future__ import annotations

import asyncio
import re
import sys

import pytest
from telegram import Bot, Update
from telegram.ext import CommandHandler

from coclody.config import ClaudeConfig, Config, TelegramConfig, UpdatesConfig
from coclody.db import connection_factory, repo
from coclody.orchestrator.worker import CancelRegistry, Worker
from coclody.security import ChatGuard, close_security_log, configure_security_log
from coclody.telegram_bot.app import Services, announce_update, build_application, setup_menu
from coclody.telegram_bot.commands import STATUS_EMOJI, STATUS_LABELS, WORD_JOINER, Commands, label, no_autolink
from coclody.telegram_bot.keyboards import BOT_COMMANDS, BUTTON_HELP, BUTTON_LIST, BUTTON_NEW
from coclody.telegram_bot.notifier import flush_notifications
from coclody.updater.client import UpdateResult
from tests.fake_telegram import TOKEN, FakeTelegram, bot_message, callback_update, inline_data, markup, user_text

ME, BRO = 6956605022, 7000000001
REQUESTED = ("queued", "running", "waiting_input", "done", "failed", "cancelled")
# Lo que Telegram toma como inicio de un link, mención, hashtag, cashtag o comando.
LINKABLE = re.compile(r"[.@#$/:](?![\s\u2060]|$)")


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
    config = Config(TelegramConfig(frozenset({ME, BRO})), cfg, UpdatesConfig(False, None, 24))
    services = Services(config=config, db=db, worker=Worker(cfg, db, CancelRegistry()), guard=ChatGuard({ME, BRO}))
    fake = FakeTelegram()
    app = build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())
    yield services, db, conn, fake, app
    close_security_log()


def run(coro):
    return asyncio.run(coro)


async def process(app, fake, update) -> list[dict]:
    before = len(fake.calls)
    await app.process_update(Update.de_json(update, app.bot))
    return [p for n, p in fake.calls[before:] if n == "sendMessage"]


def is_main_keyboard(params) -> bool:
    m = markup(params) or {}
    labels = [b["text"] if isinstance(b, dict) else b for row in m.get("keyboard", []) for b in row]
    return labels == [BUTTON_NEW, BUTTON_LIST, BUTTON_HELP] and m.get("is_persistent") is True


def task_in(conn, title, status):
    t = repo.create_task(conn, title, created_by=ME)
    path = {"pending": [], "queued": ["queued"], "running": ["queued", "running"],
            "waiting_input": ["queued", "running", "waiting_input"], "done": ["queued", "running", "done"],
            "failed": ["queued", "running", "failed"], "cancelled": ["cancelled"]}[status]
    if status == "waiting_input":
        repo.transition_task(conn, t.id, "queued")
        repo.transition_task(conn, t.id, "running")
        repo.ask_question(conn, t.id, "¿Sigo?")
        return repo.transition_task(conn, t.id, "waiting_input")
    for step in path:
        t = repo.transition_task(conn, t.id, step)
    return t


def finished(conn, title, status):
    t = task_in(conn, title, "running")
    r = repo.start_run(conn, t.id)
    repo.finish_run(conn, r.id, "ok" if status == "done" else "error", summary="s")
    return repo.transition_task(conn, t.id, status)


# --- 1. menú de comandos y teclado fijo ----------------------------------------------------------------


def test_menu_is_registered_per_whitelisted_chat_only():
    fake = FakeTelegram()

    async def go():
        async with Bot(TOKEN, request=fake) as bot:
            return await setup_menu(bot, {ME, BRO})

    assert run(go()) == 2
    scopes = [p["scope"] for p in fake.called("setMyCommands")]
    assert scopes == [{"type": "chat", "chat_id": ME}, {"type": "chat", "chat_id": BRO}]  # nunca global
    commands = fake.called("setMyCommands")[0]["commands"]
    assert [c["command"] for c in commands] == ["nueva", "lista", "estado", "responder", "cancelar", "reintentar",
                                                "ayuda"]
    buttons = fake.called("setChatMenuButton")
    assert [(p["chat_id"], p["menu_button"]) for p in buttons] == [(ME, {"type": "commands"}), (BRO, {"type": "commands"})]


def test_menu_error_on_one_chat_does_not_block_the_others(caplog):
    fake = FakeTelegram()
    fake.fail_next("setMyCommands", 400, "Bad Request: chat not found")

    async def go():
        async with Bot(TOKEN, request=fake) as bot:
            return await setup_menu(bot, {ME, BRO})

    assert run(go()) == 1
    assert [p["chat_id"] for p in fake.called("setChatMenuButton")] == [BRO]
    assert "No se pudo registrar el menú" in caplog.text


def test_every_menu_command_has_a_handler(env):
    _, _, _, _, app = env
    handled = set().union(*(h.commands for h in app.handlers[0] if isinstance(h, CommandHandler)))
    assert {c.command for c in BOT_COMMANDS} <= handled


def test_every_plain_reply_carries_the_main_keyboard(env):
    services, db, conn, fake, app = env
    done = task_in(conn, "lista", "done")
    repo.add_event(conn, done.id, "progress", "x")

    async def scenario():
        await app.initialize()
        out = []
        for text in ("/ayuda", "/cancelar 99", "/responder", "hola", BUTTON_HELP):
            out += await process(app, fake, user_text(ME, text))
        out += await process(app, fake, callback_update(ME, f"t:r:{done.id}", message=bot_message(1, ME, "x")))
        before = len(fake.sent())
        await announce_update(app.bot, services, UpdateResult("available", "9.9.9", "Hay una versión nueva."))
        out += fake.sent()[before:]
        await app.shutdown()
        return out

    sent = run(scenario())
    assert len(sent) == 8  # 5 respuestas, el resultado del botón y el aviso a los dos chats
    assert all(is_main_keyboard(p) for p in sent), [p["text"][:40] for p in sent if not is_main_keyboard(p)]


def test_messages_with_inline_buttons_keep_them(env):
    _, db, conn, fake, app = env
    task_in(conn, "fallida", "failed")

    async def scenario():
        await app.initialize()
        created = await process(app, fake, user_text(ME, "/nueva Otra"))
        await flush_notifications(app.bot, db, {ME})
        await app.shutdown()
        return created + fake.sent()[-1:]

    created, failed = run(scenario())
    assert inline_data(created) == ["t:c:2"] and inline_data(failed) == ["t:r:1"]


def test_done_notification_carries_the_main_keyboard(env):
    _, db, conn, fake, app = env
    finished(conn, "lista", "done")

    async def scenario():
        await app.initialize()
        await flush_notifications(app.bot, db, {ME})
        await app.shutdown()

    run(scenario())
    (params,) = fake.sent()
    assert is_main_keyboard(params)


# --- 2. emojis de estado -------------------------------------------------------------------------------


def test_each_status_has_its_own_emoji():
    assert set(STATUS_EMOJI) == set(STATUS_LABELS)
    assert len(set(STATUS_EMOJI.values())) == len(STATUS_EMOJI)
    for status in REQUESTED:
        assert label(status) == f"{STATUS_EMOJI[status]} {STATUS_LABELS[status]}"


def test_list_shows_the_emoji_of_each_status(env):
    services, _, conn, _, _ = env
    for status in REQUESTED:
        task_in(conn, f"tarea {status}", status)
    text = services.commands.list_tasks()
    for status in REQUESTED:
        (line,) = [ln for ln in text.splitlines() if f"tarea {status}" in ln]
        assert line.startswith(STATUS_EMOJI[status] + " #"), line
        assert line.endswith("· " + STATUS_LABELS[status])


@pytest.mark.parametrize("status", REQUESTED)
def test_status_command_shows_the_emoji(env, status):
    services, _, conn, _, _ = env
    t = task_in(conn, "x", status)
    assert f"Estado: {STATUS_EMOJI[status]} {STATUS_LABELS[status]}" in services.commands.status(str(t.id))


def test_notifications_use_the_same_emojis(env):
    _, db, conn, fake, app = env
    for status in ("done", "failed"):
        finished(conn, status, status)
    task_in(conn, "pregunta", "waiting_input")

    async def scenario():
        await app.initialize()
        await flush_notifications(app.bot, db, {ME})
        await app.shutdown()

    run(scenario())
    first = [p["text"].split(" ", 1)[0] for p in fake.sent()]
    assert first == [STATUS_EMOJI["done"], STATUS_EMOJI["failed"], STATUS_EMOJI["waiting_input"]]


def test_help_explains_the_emojis():
    help_text = Commands.help(None)
    assert all(label(status) in help_text for status in REQUESTED)


# --- 3. títulos sin autolinks ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["README.md", "Revisar https://example.com/a?b=1", "avisarle a @juan", "ver #urgente", "precio en $USD",
     "correr /cancelar 3", "mail a yo@example.com", "app.config.ts y setup.py", "v1.2.3 en C:\\x"],
)
def test_no_autolink_leaves_nothing_linkable_and_looks_the_same(raw):
    safe = no_autolink(raw)
    assert LINKABLE.search(safe) is None
    assert safe.replace(WORD_JOINER, "") == raw  # se ve igual: solo se agregan caracteres invisibles


@pytest.mark.parametrize("raw", ["Arreglar login", "Fin de frase.", "Hora: 10", "a / b", "precio $ 10"])
def test_no_autolink_does_not_touch_plain_text(raw):
    assert no_autolink(raw) == raw


def test_titles_are_escaped_everywhere_but_stored_as_is(env):
    services, db, conn, fake, app = env

    async def scenario():
        await app.initialize()
        created = await process(app, fake, user_text(ME, "/nueva Actualizar README.md\nVer docs/README.md"))
        listing = await process(app, fake, user_text(ME, "/lista"))
        status = await process(app, fake, user_text(ME, "/estado 1"))
        repo.transition_task(conn, 1, "running")
        r = repo.start_run(conn, 1)
        repo.finish_run(conn, r.id, "error", summary="falló")
        repo.transition_task(conn, 1, "failed")
        await flush_notifications(app.bot, db, {ME})
        await app.shutdown()
        return [p["text"] for p in created + listing + status] + [fake.sent()[-1]["text"]]

    texts = run(scenario())
    escaped = no_autolink("Actualizar README.md")
    assert len(texts) == 4 and all(escaped in t for t in texts)
    assert not any("README.md" in t for t in texts), [t for t in texts if "README.md" in t]
    assert repo.get_task(conn, 1).title == "Actualizar README.md"  # la base guarda el original


# --- ningún autolink en ninguna parte del chat --------------------------------------------------------

# Aproximación a lo que Telegram linkea solo: dominios, URLs, menciones, hashtags con letras,
# cashtags y comandos. "#12" (número de tarea) no es hashtag para Telegram: lleva letras.
REAL_LINK = re.compile(r"[\w-]\.[A-Za-z]{2,}|://|@\w|#\w*[^\W\d]|\$[A-Z]{3,8}|(?<![\w/])/\w")
# Los comandos que el propio bot sugiere ("mandá /responder 3 …") se dejan tocables a propósito.
OWN_COMMANDS = re.compile(r"(?<![\w/])/(?:nueva|lista|estado|responder|cancelar|reintentar|ayuda|start)\b")
LINKY = "README.md, https://example.com/x, @juan, #urgente, $USD y /borrar_todo"


def test_no_text_from_claude_or_the_user_is_linkable_anywhere(env):
    services, db, conn, fake, app = env
    done = finished(conn, f"Hecha {LINKY}", "done")
    repo.add_event(conn, done.id, "progress", f"Avance: edité {LINKY}")
    conn.execute("UPDATE runs SET summary = ? WHERE task_id = ?", (f"Resumen: {LINKY}", done.id))
    failed = finished(conn, f"Rota {LINKY}", "failed")
    conn.execute("UPDATE runs SET summary = ? WHERE task_id = ?", (f"Error: {LINKY}", failed.id))
    waiting = task_in(conn, "Pregunta", "running")
    repo.ask_question(conn, waiting.id, f"¿Toco {LINKY}?", kind="yes_no")
    repo.transition_task(conn, waiting.id, "waiting_input")
    conn.execute("UPDATE tasks SET workdir_rel = 'web.app' WHERE id = ?", (done.id,))

    async def scenario():
        await app.initialize()
        await flush_notifications(app.bot, db, {ME})
        for text in ("/lista", f"/estado {done.id}", f"/estado {failed.id}", f"/estado {waiting.id}",
                     f"/nueva Nueva {LINKY}\nDescripción {LINKY}", "/estado 4", "/nueva " + "x" * 300):
            await process(app, fake, user_text(ME, text))
        await announce_update(app.bot, services, UpdateResult("available", f"Versión 1.2.3 en {LINKY}", "1.2.3"))
        await app.shutdown()
        return [p["text"] for p in fake.sent()]

    texts = run(scenario())
    assert len(texts) >= 10
    offenders = [(m.group(0), t[:60]) for t in texts for m in REAL_LINK.finditer(OWN_COMMANDS.sub("", t))]
    assert offenders == []
    joined = "\n".join(texts).replace(WORD_JOINER, "")
    for piece in ("Resumen: ", "Error: ", "Avance: edité ", "¿Toco ", "Descripción ", "web.app"):
        assert piece in joined  # el contenido sí llegó (y se ve igual), solo que sin links


def test_coclody_never_writes_first(env):
    """Al arrancar no se manda ningún mensaje: solo se registra el menú. Solo escriben los avisos de tareas."""
    services, db, conn, fake, app = env
    from coclody.telegram_bot import app as app_module

    async def scenario():
        await app.initialize()
        await app_module._post_init(app)  # arranque real de los bucles, sin tareas ni avisos pendientes
        await asyncio.sleep(0.2)
        await app_module._stop_services(app)
        await app.shutdown()

    run(scenario())
    assert fake.sent() == []
    assert {name for name, _ in fake.calls} <= {"getMe", "setMyCommands", "setChatMenuButton"}
