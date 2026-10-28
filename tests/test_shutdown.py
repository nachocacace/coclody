"""Apagado real de run_polling, sin reemplazarlo por un stub.

En producción, el polling terminó con InvalidToken y python-telegram-bot volvió a lanzar ese error
dentro de su propio finally, salteando stop, post_stop y shutdown: no se canceló la tarea en
curso, no se enviaron los avisos pendientes y las tareas de fondo se destruyeron pendientes. Los
tests anteriores reemplazaban run_polling, así que nunca pasaban por ese camino.

Acá corre el run_polling de verdad contra el cliente simulado: el worker está ejecutando una tarea
(un Claude falso que espera hasta que lo cancelen) cuando el polling muere o se pide parar.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time

import pytest
from telegram.error import InvalidToken

from coclody.config import ClaudeConfig, Config, TelegramConfig, UpdatesConfig
from coclody.db import connection_factory, repo
from coclody.orchestrator.claude_runner import ClaudeResult
from coclody.orchestrator.worker import CancelRegistry, Worker
from coclody.security import ChatGuard, close_security_log, configure_security_log
from coclody.telegram_bot import app as app_module
from coclody.telegram_bot.app import Services, build_application
from tests.fake_telegram import TOKEN, FakeTelegram

ME = 6956605022


class BlockingClaude:
    """Claude falso que trabaja hasta que lo cancelan (o hasta 10 s, para no colgar el test)."""

    def __init__(self):
        self.started = threading.Event()
        self.cancelled = False

    def __call__(self, inv, cancel):
        self.started.set()
        self.cancelled = cancel.wait(10)
        return ClaudeResult(status="killed" if self.cancelled else "ok", exit_code=None, session_id=None,
                            result_text=None, summary="cortada", stdout_truncated=False, duration_s=0.1)


class Polling(FakeTelegram):
    """getUpdates: cuando el worker ya arrancó la tarea, responde ``status`` y pide parar la app."""

    def __init__(self, claude: BlockingClaude, status: int):
        super().__init__()
        self.claude, self.status, self.app, self.stop_requested = claude, status, None, False

    async def do_request(self, url, method, request_data=None, **kw):
        if url.endswith("/getUpdates"):
            while not self.claude.started.is_set():
                await asyncio.sleep(0.01)
            if not self.stop_requested:  # una sola vez, como un Ctrl+C
                self.stop_requested = True
                asyncio.get_running_loop().call_later(0.1, self.app.stop_running)
            if self.status == 401:
                return 401, b'{"ok": false, "error_code": 401, "description": "Unauthorized"}'
            await asyncio.sleep(0.05)
            return 200, b'{"ok": true, "result": []}'
        return await super().do_request(url, method, request_data, **kw)


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
    claude = BlockingClaude()
    services = Services(
        config=Config(TelegramConfig(frozenset({ME})), cfg, UpdatesConfig(False, None, 24)),
        db=db,
        worker=Worker(cfg, db, CancelRegistry(), claude),
        guard=ChatGuard({ME}),
    )
    t = repo.create_task(conn, "larga", created_by=ME)
    repo.transition_task(conn, t.id, "queued")
    yield services, claude, conn
    close_security_log()


def start(services, claude, status):
    api, polling = FakeTelegram(), Polling(claude, status)
    application = build_application(TOKEN, services, request=api, get_updates_request=polling)
    polling.app = application
    return application, api


def assert_cleaned_up(services, claude, conn, api):
    assert claude.cancelled  # la tarea en curso se canceló por el apagado, no por el timeout del test
    task = repo.get_task(conn, 1)
    assert task.status == "failed"
    assert "Interrumpida al cerrar CoClody." in [e.message for e in repo.list_events(conn, 1)]
    assert any("❌ #1" in m["text"] and "falló" in m["text"] for m in api.sent())  # el aviso pendiente salió
    assert services.background and all(t.done() and not t.cancelled() for t in services.background)


def test_cleanup_runs_when_polling_dies_with_invalid_token(setup, caplog):
    services, claude, conn = setup
    application, api = start(services, claude, 401)
    began = time.monotonic()
    with pytest.raises(InvalidToken):  # el error se sigue informando
        app_module.run_polling(application)
    assert time.monotonic() - began < 8
    assert_cleaned_up(services, claude, conn, api)
    assert not application.running
    assert "Task was destroyed but it is pending" not in caplog.text


def test_normal_stop_cleans_up_once(setup, monkeypatch):
    services, claude, conn = setup
    calls = []
    original = app_module.flush_notifications

    async def counting_flush(*args, **kwargs):
        calls.append(services.stopped)
        return await original(*args, **kwargs)

    monkeypatch.setattr(app_module, "flush_notifications", counting_flush)
    application, api = start(services, claude, 200)
    app_module.run_polling(application)
    assert_cleaned_up(services, claude, conn, api)
    # Una sola limpieza: post_stop la hace y la de run_polling no repite el envío final.
    assert calls.count(True) == 1
    # Al arrancar (post_init) se registró el menú de comandos del chat autorizado.
    assert [p["scope"] for p in api.called("setMyCommands")] == [{"type": "chat", "chat_id": ME}]


# --- el polling muere y CoClody sigue abierto ------------------------------------------------------


class QuickClaude:
    def __init__(self):
        self.started = threading.Event()
        self.runs = 0

    def __call__(self, inv, cancel):
        self.started.set()
        self.runs += 1
        return ClaudeResult(status="ok", exit_code=0, session_id=None, result_text="Listo.", summary="Listo.",
                            stdout_truncated=False, duration_s=0.1)


class DeadPolling(FakeTelegram):
    """getUpdates siempre responde 401 (token revocado). No para la app: eso lo decide el test."""

    async def do_request(self, url, method, request_data=None, **kw):
        if url.endswith("/getUpdates"):
            return 401, b'{"ok": false, "error_code": 401, "description": "Unauthorized"}'
        return await super().do_request(url, method, request_data, **kw)


def test_dead_polling_is_reported_and_leaves_a_clear_paused_state(setup, monkeypatch, caplog):
    services, _, conn = setup
    for name in ("POLL_WATCH_S", "WORKER_IDLE_S", "NOTIFY_INTERVAL_S"):
        monkeypatch.setattr(app_module, name, 0.05)
    claude = QuickClaude()
    services.worker.runner = claude
    api = FakeTelegram()
    application = build_application(TOKEN, services, request=api, get_updates_request=DeadPolling())
    seen = {}

    def observer(loop_holder):
        # Espera a que el vigilante marque el polling como muerto, encola trabajo nuevo y mira que no avance.
        deadline = time.monotonic() + 10
        while services.polling_error is None and time.monotonic() < deadline:
            time.sleep(0.02)
        seen["error"] = services.polling_error
        seen["sends_before"], seen["runs_before"] = len(api.sent()), claude.runs
        with services.db() as c:  # conexión propia del hilo
            t = repo.create_task(c, "nueva después de morir", created_by=ME)
            repo.transition_task(c, t.id, "queued")
        time.sleep(0.5)  # varias vueltas del worker y del notificador
        with services.db() as c:
            seen["new_status"] = repo.get_task(c, t.id).status
        seen["sends_after"] = len(api.sent())
        seen["runs"] = claude.runs
        loop_holder[0].call_soon_threadsafe(application.stop_running)

    holder = []
    original_post_init = application.post_init

    async def post_init(app):
        holder.append(asyncio.get_running_loop())
        threading.Thread(target=observer, args=(holder,), daemon=True).start()
        await original_post_init(app)

    application.post_init = post_init
    with caplog.at_level(logging.INFO):
        with pytest.raises(InvalidToken):
            app_module.run_polling(application)

    assert seen["error"] == "InvalidToken: Unauthorized"
    assert seen["new_status"] == "queued" and seen["runs"] == seen["runs_before"]  # no empezó nada nuevo
    assert seen["sends_after"] == seen["sends_before"]  # el notificador quedó en pausa
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "El polling de Telegram se detuvo" in r.getMessage()]
    assert len(errors) == 1  # una sola vez, no en cada vuelta
    message = errors[0].getMessage()
    assert "NO recibe mensajes" in message and "coclody check" in message and "reiniciá" in message
    assert not any(m["text"].startswith("El polling") for m in api.sent())  # CoClody no escribe primero


def test_watchdog_does_not_fire_on_a_normal_stop(setup):
    services, claude, conn = setup
    application, api = start(services, claude, 200)
    app_module.run_polling(application)
    assert services.polling_error is None


def test_watchdog_warns_if_it_cannot_see_the_polling_task(monkeypatch):
    from types import SimpleNamespace

    fake_app = SimpleNamespace(updater=SimpleNamespace(running=True))
    assert app_module.polling_failure(fake_app) == app_module._UNWATCHABLE
    fake_app.updater.running = False
    assert app_module.polling_failure(fake_app) is None
