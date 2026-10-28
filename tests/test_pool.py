"""Etapa 2 del orquestador: workers en worktrees, commits hechos por CoClody e invariantes de git.

Claude Code es simulado (un runner que escribe archivos en su carpeta de trabajo, o que hace lo
que un worker no debería: correr git), pero git es real. Lo que se verifica:
- cada tarea corre en el worktree de su worker, con herramientas acotadas y entorno sin secretos;
- CoClody commitea en la rama del worker, sin hooks, y registra archivos y líneas;
- si algo cambió git por fuera de CoClody, la tarea falla, el worker se detiene y el aviso lo dice.
"""

from __future__ import annotations

import asyncio
import os
import sys

import pytest

from coclody.config import load_config
from coclody.db import connection_factory, repo
from coclody.db import projects as prepo
from coclody.orchestrator import projects as project_ops
from coclody.orchestrator.claude_runner import ClaudeResult
from coclody.orchestrator.pool import commit_message, worker_env, worker_tools
from coclody.orchestrator.worker import CancelRegistry, Worker
from coclody.security import ChatGuard, close_security_log, configure_security_log
from coclody.telegram_bot import app as app_module
from coclody.telegram_bot.app import Services
from coclody.telegram_bot.notifier import flush_notifications
from tests.gitrepo import make_git, make_repo, needs_git, sh_git

pytestmark = needs_git
posix_only = pytest.mark.skipif(os.name != "posix", reason="hooks ejecutables: solo POSIX")
ME = 111


def run_once(services, name):
    """run_worker_once devuelve (tarea, usage_limited); los tests solo miran la tarea."""
    task, _ = app_module.run_worker_once(services, name)
    return task


def ok(text="Listo.\nRESUMEN\nHice algo."):
    return ClaudeResult(status="ok", exit_code=0, session_id=None, result_text=text, summary=text,
                        stdout_truncated=False, duration_s=0.1)


class FakeClaude:
    """Runner simulado. ``action(cwd)`` hace el trabajo; guarda cada invocación para inspeccionarla."""

    def __init__(self, action=None):
        self.action = action or (lambda cwd: None)
        self.invocations = []

    def __call__(self, inv, cancel):
        self.invocations.append(inv)
        self.action(inv.cwd)
        return ok()


@pytest.fixture
def env(app_paths, conn, tmp_path, monkeypatch):
    # worker_commands se acepta corra donde corra el CI (el gate sin sandbox tiene su propio test).
    monkeypatch.setattr("coclody.config._sandbox_platform", lambda: True)
    ws = (tmp_path / "ws")
    ws.mkdir()
    ws = ws.resolve()
    exe = tmp_path / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    exe.parent.mkdir()
    exe.write_bytes(b"")
    exe.chmod(0o755)
    root = make_repo(ws / "proj")
    configure_security_log(app_paths.security_log)

    def build(extra="", action=None):
        app_paths.config_file.write_text(
            f'[telegram]\nallowed_chat_ids = [{ME}]\n[claude]\nworkspace_root = "{str(ws).replace(chr(92), chr(92) * 2)}"\n'
            f'executable = "{str(exe).replace(chr(92), chr(92) * 2)}"\n[orquestador]\nenabled = true\n{extra}',
            encoding="utf-8",
        )
        config = load_config(app_paths)
        db = connection_factory(app_paths.db_file)
        git = make_git(app_paths.git_hooks_dir)
        if prepo.get_project(conn, "web") is None:
            project_ops.add(db, git, config, "web", root)
        project_ops.activate(db, git, config, "web")
        claude = FakeClaude(action)
        services = Services(config=config, db=db, worker=Worker(config.claude, db, CancelRegistry(), claude),
                            guard=ChatGuard({ME}), git=git, app_paths=app_paths)
        return services, claude

    yield build, root, conn
    close_security_log()


def queue(conn, title, worker, created_by=ME):
    project = prepo.active_project(conn)
    t = repo.create_task(conn, title, created_by=created_by, project_id=project.id)
    return repo.assign_task(conn, t.id, project.id, worker)


def wt(root, name):
    return root / ".coclody" / "worktrees" / name


def write_file(name, content="nuevo\n"):
    def action(cwd):
        (cwd / name).write_text(content, encoding="utf-8")

    return action


# --- una tarea de punta a punta ------------------------------------------------------------------------


def test_task_runs_in_its_worktree_and_coclody_commits_it(env, app_paths):
    build, root, conn = env
    services, claude = build(action=write_file("hecho.txt"))
    main_before = sh_git(root, "rev-parse", "main").strip()
    task = queue(conn, "Agregar hecho.txt", "w1")

    final = run_once(services, "w1")
    assert final.status == "done"
    (inv,) = claude.invocations
    assert inv.cwd == wt(root, "w1").resolve()
    # El commit lo hizo CoClody, en la rama del worker; main y la raíz no cambiaron.
    log = sh_git(wt(root, "w1"), "log", "-1", "--format=%s%n%b")
    assert log.startswith("coclody(w1): Agregar hecho.txt")
    assert f"CoClody-Task: {task.id}" in log and "CoClody-Worker: w1" in log and "CoClody-Status: ok" in log
    assert sh_git(root, "rev-parse", "main").strip() == main_before
    assert not (root / "hecho.txt").exists()
    assert sh_git(wt(root, "w1"), "status", "--porcelain") == ""
    (commit,) = conn.execute("SELECT files, insertions, deletions, big_change, worker FROM task_commits").fetchall()
    assert tuple(commit) == (1, 1, 0, 0, "w1")
    # Respaldo del HEAD anterior a la tarea.
    backups = sh_git(root, "for-each-ref", "--format=%(refname) %(objectname)", "refs/coclody/backup/w1")
    assert backups.strip().endswith(main_before)
    project = prepo.active_project(conn)
    assert prepo.get_worker(conn, project.id, "w1").state == "idle"


def test_task_without_changes_makes_no_commit(env):
    build, root, conn = env
    services, _ = build()
    head = sh_git(wt(root, "w1"), "rev-parse", "HEAD").strip()
    queue(conn, "Solo leer", "w1")
    assert run_once(services, "w1").status == "done"
    assert sh_git(wt(root, "w1"), "rev-parse", "HEAD").strip() == head
    assert conn.execute("SELECT count(*) FROM task_commits").fetchone()[0] == 0


def test_each_worker_only_takes_its_own_tasks_in_its_own_worktree(env):
    build, root, conn = env
    services, claude = build(action=write_file("marca.txt"))
    queue(conn, "para w2", "w2")
    assert run_once(services, "w1") is None  # w1 no tiene nada
    assert run_once(services, "w2").title == "para w2"
    assert [inv.cwd for inv in claude.invocations] == [wt(root, "w2").resolve()]
    assert (wt(root, "w2") / "marca.txt").exists() and not (wt(root, "w1") / "marca.txt").exists()
    assert sh_git(root, "log", "-1", "--format=%s", "coclody/w2").startswith("coclody(w2)")
    assert sh_git(root, "rev-parse", "coclody/w1").strip() == sh_git(root, "rev-parse", "main").strip()


def test_invocation_is_scoped_and_env_has_no_secrets(env, monkeypatch, app_paths):
    build, root, conn = env
    for name, value in {"SSH_AUTH_SOCK": "/tmp/agent", "GH_TOKEN": "gh", "MY_SECRET": "x", "AWS_SECRET_ACCESS_KEY": "y",
                        "GIT_DIR": "/otro", "CLAUDE_CODE_OAUTH_TOKEN": "oauth", "PATH": os.environ["PATH"]}.items():
        monkeypatch.setenv(name, value)
    services, claude = build('worker_commands = ["pytest", "npm test"]\n')
    queue(conn, "t", "w1")
    run_once(services, "w1")
    (inv,) = claude.invocations
    argv = list(inv.argv)
    allowed = argv[argv.index("--allowedTools") + 1].split(",")
    assert allowed == ["Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./**)", "Write(./**)", "Bash(pytest:*)",
                       "Bash(npm test:*)"]
    assert argv[argv.index("--tools") + 1] == "Read,Glob,Grep,Edit,Write,Bash"
    assert not any(t in ("Bash", "Bash(git:*)") or t.startswith("Bash(git") for t in allowed)
    e = inv.env
    assert not {"SSH_AUTH_SOCK", "GH_TOKEN", "MY_SECRET", "AWS_SECRET_ACCESS_KEY", "GIT_DIR"} & set(e)
    assert e["CLAUDE_CODE_OAUTH_TOKEN"] == "oauth"  # Claude Code la necesita para autenticarse
    assert e["GIT_CONFIG_NOSYSTEM"] == "1" and e["GIT_CONFIG_GLOBAL"] == str(app_paths.git_empty_config)
    pairs = {e[f"GIT_CONFIG_KEY_{i}"]: e[f"GIT_CONFIG_VALUE_{i}"] for i in range(int(e["GIT_CONFIG_COUNT"]))}
    assert pairs["core.hooksPath"] == str(app_paths.git_hooks_dir) and pairs["protocol.allow"] == "never"
    assert pairs["credential.helper"] == ""
    prompt = inv.stdin.decode()
    assert "No uses git" in prompt and "pytest, npm test" in prompt and "RESUMEN" in prompt and "w1" in prompt


def test_without_worker_commands_there_is_no_bash(env):
    build, root, conn = env
    services, claude = build()
    queue(conn, "t", "w1")
    run_once(services, "w1")
    argv = list(claude.invocations[0].argv)
    assert "Bash" not in argv[argv.index("--tools") + 1]
    assert "No podés correr comandos" in claude.invocations[0].stdin.decode()


def test_big_change_is_flagged_in_the_notification(env):
    build, root, conn = env

    def many(cwd):
        for i in range(3):
            (cwd / f"f{i}.txt").write_text("a\nb\n", encoding="utf-8")

    services, _ = build("big_change_files = 2\n", action=many)
    queue(conn, "Muchos archivos", "w1")
    run_once(services, "w1")
    assert conn.execute("SELECT files, insertions, big_change FROM task_commits").fetchone()[:] == (3, 6, 1)
    from tests.fake_telegram import TOKEN, FakeTelegram
    from telegram import Bot

    fake = FakeTelegram()

    async def go():
        async with Bot(TOKEN, request=fake) as bot:
            await flush_notifications(bot, services.db, {ME})

    asyncio.run(go())
    (msg,) = fake.sent()
    assert "3 arch +6 −0" in msg["text"] and "⚠️ cambio grande" in msg["text"]


@posix_only
def test_repo_hooks_never_run_on_coclody_commits(env, tmp_path):
    build, root, conn = env
    marker = tmp_path / "hook-corrio"
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    for name in ("pre-commit", "commit-msg", "post-commit"):
        (hooks / name).write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
        (hooks / name).chmod(0o755)
    sh_git(root, "config", "core.hooksPath", str(hooks))
    services, _ = build(action=write_file("x.txt"))
    queue(conn, "t", "w1")
    assert run_once(services, "w1").status == "done"
    assert not marker.exists()


def test_commit_message_strips_control_chars_and_keeps_trailers_last():
    task = repo.Task(id=7, title="Título\x1b[31m rojo\nsegunda", description=None, status="running", priority=0,
                     workdir_rel=None, created_by=None, created_at="", updated_at="", finished_at=None)
    message = commit_message(task, "w1", ok("Hice\x00 algo\x07.\n\nSigned-off-by: alguien"))
    assert "\x1b" not in message and "\x00" not in message and "\x07" not in message
    assert message.splitlines()[0] == "coclody(w1): Título[31m rojo segunda"
    assert message.rstrip().endswith("CoClody-Task: 7\nCoClody-Worker: w1\nCoClody-Status: ok")


def test_worker_tools_and_env_helpers(tmp_path):
    assert worker_tools(()) == ("Read(./**)", "Glob(./**)", "Grep(./**)", "Edit(./**)", "Write(./**)")
    e = worker_env({"HOME": "/h", "GIT_WORK_TREE": "/x", "DB_PASSWORD": "p", "github_token": "t"}, tmp_path / "h",
                   tmp_path / "c")
    assert e["HOME"] == "/h" and not {"GIT_WORK_TREE", "DB_PASSWORD", "github_token"} & set(e)


# --- invariantes: git tocado por fuera de CoClody ---------------------------------------------------------


def violation(env, action, title="rebelde"):
    build, root, conn = env
    services, claude = build(action=action)
    task = queue(conn, title, "w1")
    final = run_once(services, "w1")
    project = prepo.active_project(conn)
    events = [e.message for e in repo.list_events(conn, task.id)]
    return services, final, prepo.get_worker(conn, project.id, "w1").state, events, root, conn


def test_worker_that_commits_by_itself_is_stopped(env):
    def rogue(cwd):
        (cwd / "a.txt").write_text("x", encoding="utf-8")
        sh_git(cwd, "add", "a.txt")
        sh_git(cwd, "commit", "-q", "-m", "commit propio")

    services, final, state, events, root, conn = violation(env, rogue)
    assert final.status == "failed" and state == "error"
    assert any("HEAD del worktree cambió por fuera de CoClody" in e for e in events)
    queue(conn, "otra", "w1")
    assert run_once(services, "w1") is None  # el worker no toma más tareas


def test_moving_main_is_detected(env):
    def rogue(cwd):
        # Mueve main a un commit nuevo sin tocar el HEAD del worker (lo que haría un "git push . HEAD:main").
        sha = sh_git(cwd, "commit-tree", "HEAD^{tree}", "-p", "HEAD", "-m", "colado en main").strip()
        sh_git(cwd, "update-ref", "refs/heads/main", sha)

    _, final, state, events, root, _ = violation(env, rogue)
    assert final.status == "failed" and state == "error"
    assert any("ramas que no son de workers: main" in e for e in events)


def test_deleting_another_branch_is_detected(env):
    build, root, conn = env
    sh_git(root, "branch", "feature-ajena")

    def rogue(cwd):
        sh_git(cwd, "branch", "-D", "feature-ajena")

    _, final, state, events, _, _ = violation(env, rogue)
    assert final.status == "failed" and state == "error"
    assert any("feature-ajena" in e for e in events)


def test_creating_a_worktree_is_detected(env, tmp_path):
    def rogue(cwd):
        sh_git(cwd, "worktree", "add", "-q", "-b", "escape", str(tmp_path / "escape"))

    _, final, state, events, _, _ = violation(env, rogue)
    assert final.status == "failed" and state == "error"
    assert any("lista de worktrees" in e for e in events)


def test_worktree_on_wrong_branch_never_runs_claude(env):
    build, root, conn = env
    services, claude = build()
    sh_git(wt(root, "w1"), "checkout", "-q", "-b", "desvio")
    task = queue(conn, "t", "w1")
    final = run_once(services, "w1")
    assert final.status == "failed" and claude.invocations == []
    assert prepo.get_worker(conn, prepo.active_project(conn).id, "w1").state == "error"
    assert any("antes de empezar" in e.message for e in repo.list_events(conn, task.id))


def test_failed_notification_explains_the_violation(env):
    def rogue(cwd):
        sh_git(cwd, "commit", "-q", "--allow-empty", "-m", "x")

    services, *_ = violation(env, rogue)
    from tests.fake_telegram import TOKEN, FakeTelegram
    from telegram import Bot

    fake = FakeTelegram()

    async def go():
        async with Bot(TOKEN, request=fake) as bot:
            await flush_notifications(bot, services.db, {ME})

    asyncio.run(go())
    (msg,) = fake.sent()
    assert "falló" in msg["text"]  # una línea; el detalle (la invariante) va detrás de Detalle y en el evento
    with services.db() as c:
        t = repo.list_tasks(c)[0]
        assert any("Invariante de git violada en w1" in e.message for e in repo.list_events(c, t.id))


def test_restart_resets_worker_state(env):
    from coclody.main import prepare_orchestrator

    def rogue(cwd):
        sh_git(cwd, "commit", "-q", "--allow-empty", "-m", "x")

    services, _, state, _, root, conn = violation(env, rogue)
    assert state == "error"
    sh_git(wt(root, "w1"), "reset", "-q", "--hard", "HEAD~1")  # la persona revisa y arregla
    prepare_orchestrator(services.config, services.app_paths, services.db)
    assert prepo.get_worker(conn, prepo.active_project(conn).id, "w1").state == "idle"


# --- Telegram: /nueva con worker y botones de asignación ------------------------------------------------------


def telegram(services, *updates):
    from telegram import Update

    from tests.fake_telegram import TOKEN, FakeTelegram

    fake = FakeTelegram()
    app = app_module.build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())
    out = []

    async def go():
        await app.initialize()
        for make in updates:
            before = len(fake.sent())
            await app.process_update(Update.de_json(make(fake), app.bot))
            out.append(fake.sent()[before:])
        await app.shutdown()

    asyncio.run(go())
    return out


def test_new_task_for_a_named_worker(env):
    from tests.fake_telegram import user_text

    build, root, conn = env
    services, _ = build()
    (sent,) = telegram(services, lambda f: user_text(ME, "/nueva w2 Arreglar el login\nEn Safari"))
    assert sent[0]["text"].startswith("Creé la tarea #1 para w2: Arreglar el login")
    t = repo.get_task(conn, 1)
    assert (t.worker, t.status, t.description, t.project_id) == ("w2", "queued", "En Safari",
                                                                 prepo.active_project(conn).id)


def test_new_task_without_worker_offers_buttons_and_assigns(env):
    from tests.fake_telegram import bot_message_with_buttons, callback_update, inline_data, user_text

    build, root, conn = env
    services, _ = build()
    created, assigned, again, bad = telegram(
        services,
        lambda f: user_text(ME, "/nueva Revisar docs"),
        lambda f: callback_update(ME, "w:1:w3", message=bot_message_with_buttons(f.returned_ids[0], ME, f.sent()[0])),
        lambda f: callback_update(ME, "w:1:w1", message=bot_message_with_buttons(f.returned_ids[0], ME, f.sent()[0])),
        lambda f: callback_update(ME, "w:1:w9", message=bot_message_with_buttons(f.returned_ids[0], ME, f.sent()[0])),
    )
    assert "¿Qué worker la hace?" in created[0]["text"]
    assert inline_data(created[0]) == ["w:1:w1", "w:1:w2", "w:1:w3"]
    assert assigned[0]["text"] == "La tarea #1 quedó en la cola de w3."
    assert "ya no está esperando asignación" in again[0]["text"]
    assert "No pude asignar" in bad[0]["text"]
    t = repo.get_task(conn, 1)
    assert (t.worker, t.status) == ("w3", "queued")


def test_list_shows_the_worker(env):
    from tests.fake_telegram import user_text

    build, root, conn = env
    services, _ = build()
    queue(conn, "una", "w2")
    ((msg,),) = telegram(services, lambda f: user_text(ME, "/lista"))
    assert "#1 [w2] una" in msg["text"]


def test_no_active_project_means_no_task(env):
    from tests.fake_telegram import user_text

    build, root, conn = env
    services, _ = build()
    conn.execute("UPDATE projects SET active = 0")
    ((msg,),) = telegram(services, lambda f: user_text(ME, "/nueva algo"))
    assert "No hay proyecto activo" in msg["text"]
    assert repo.list_tasks(conn) == []


def test_orchestrator_starts_one_loop_per_worker_and_not_the_single_worker(env):
    build, root, conn = env
    services, _ = build("workers = 2\n")
    from tests.fake_telegram import TOKEN, FakeTelegram

    app = app_module.build_application(TOKEN, services, request=FakeTelegram(), get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        await app_module._post_init(app)
        names = sorted(t.get_name() for t in services.background)
        await app_module._stop_services(app)
        await app.shutdown()
        return names

    names = asyncio.run(go())
    assert "coclody-worker-w1" in names and "coclody-worker-w2" in names and "coclody-worker" not in names


def test_two_workers_in_parallel_do_not_trip_each_other(env):
    import threading

    build, root, conn = env
    both_running = threading.Barrier(2, timeout=10)

    def action(cwd):
        both_running.wait()  # los dos Claude trabajan al mismo tiempo
        (cwd / f"{cwd.name}.txt").write_text(cwd.name, encoding="utf-8")

    services, claude = build(action=action)
    queue(conn, "uno", "w1")
    queue(conn, "dos", "w2")
    results = {}

    def run(name):
        results[name] = run_once(services, name)

    threads = [threading.Thread(target=run, args=(n,)) for n in ("w1", "w2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert {n: r.status for n, r in results.items()} == {"w1": "done", "w2": "done"}
    assert (wt(root, "w1") / "w1.txt").exists() and (wt(root, "w2") / "w2.txt").exists()
    assert not (wt(root, "w1") / "w2.txt").exists()
    states = {w.name: w.state for w in prepo.list_workers(conn, prepo.active_project(conn).id)}
    assert states == {"w1": "idle", "w2": "idle", "w3": "idle"}
    assert conn.execute("SELECT count(*) FROM task_commits").fetchone()[0] == 2


# --- límite de uso, tope de reintentos, identidad y /worker ----------------------------------------------


def usage(text="Claude usage limit reached"):
    return ClaudeResult(status="usage_limited", exit_code=1, session_id=None, result_text=text, summary=text,
                        stdout_truncated=False, duration_s=0.1)


def test_usage_limit_requeues_and_flags_without_failing(env):
    build, root, conn = env
    services, claude = build()
    claude.action = lambda cwd: None
    # El runner devuelve usage_limited.
    services.worker.runner = lambda inv, cancel: usage()
    queue(conn, "t", "w1")
    task, hit = app_module.run_worker_once(services, "w1")
    assert hit is True and task.status == "queued"  # vuelve a la cola, no falla
    assert repo.count_runs(conn, 1) == 1
    # El worker no quedó detenido (no es su culpa).
    assert prepo.get_worker(conn, prepo.active_project(conn).id, "w1").state == "idle"


def test_usage_limit_gives_up_after_max_runs(env):
    build, root, conn = env
    services, _ = build("max_task_runs = 2\n")
    services.worker.runner = lambda inv, cancel: usage()
    queue(conn, "t", "w1")
    first, _ = app_module.run_worker_once(services, "w1")
    assert first.status == "queued"
    second, _ = app_module.run_worker_once(services, "w1")
    assert second.status == "failed"  # a la segunda ejecución llega al tope
    assert any("No se reintenta" in e.message for e in repo.list_events(conn, 1))


def test_pool_loop_pauses_and_resumes_on_usage_limit(env, monkeypatch):
    import asyncio

    from tests.fake_telegram import TOKEN, FakeTelegram

    build, root, conn = env
    services, _ = build()
    services.worker.runner = lambda inv, cancel: usage()
    monkeypatch.setattr(app_module, "WORKER_IDLE_S", 0.02)
    monkeypatch.setattr(app_module, "USAGE_COOLDOWN_S", 0.3)
    queue(conn, "t", "w1")
    fake = FakeTelegram()
    app = app_module.build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        await app_module._post_init(app)
        # Espera a que la cola se pause y llegue el aviso.
        for _ in range(200):
            if services.usage_paused_at is not None and fake.sent():
                break
            await asyncio.sleep(0.02)
        paused = services.usage_paused_at is not None
        # Tras el enfriamiento, se reanuda sola.
        for _ in range(100):
            if services.usage_paused_at is None:
                break
            await asyncio.sleep(0.02)
        resumed = services.usage_paused_at is None
        await app_module._stop_services(app)
        await app.shutdown()
        return paused, resumed, [m["text"] for m in fake.sent()]

    paused, resumed, texts = asyncio.run(go())
    assert paused and resumed
    assert any("límite de uso" in t for t in texts)


def test_check_flags_missing_git_identity(env, monkeypatch, app_paths):
    from coclody import cli
    from tests.test_cli import run_check

    from coclody.orchestrator import gitops

    build, root, conn = env
    services, _ = build()
    sh_git(root, "config", "--unset", "user.name")
    sh_git(root, "config", "--unset", "user.email")
    # Que git no lea una identidad global del sistema donde corren los tests (en CI Windows no hay).
    home = app_paths.data_dir / "sin-identidad"
    home.mkdir()
    empty = home / "gitconfig"

    def no_global_env():
        e = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
        e.update({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(empty), "HOME": str(home)})
        return e

    monkeypatch.setattr(gitops, "git_env", no_global_env)

    class Store:
        def get(self, name):
            return "123:abc"

    monkeypatch.setattr(cli, "resolve_executable", lambda *a: root)
    code, text = run_check(app_paths, Store())
    assert "git no tiene user.name" in text and code == 2


def test_worker_command_lists_and_resumes(env):
    from tests.fake_telegram import user_text

    build, root, conn = env
    services, _ = build()
    project = prepo.active_project(conn)
    prepo.set_worker_state(conn, project.id, "w2", "error")
    ((listed,),) = telegram(services, lambda f: user_text(ME, "/worker"))
    assert "🟢 w1: libre" in listed["text"] and "🛑 w2: detenido" in listed["text"] and "reanuda" in listed["text"]
    ((resumed,),) = telegram(services, lambda f: user_text(ME, "/worker w2 reanudar"))
    assert resumed["text"] == "Reanudé el worker w2: vuelve a tomar tareas."
    assert prepo.get_worker(conn, project.id, "w2").state == "idle"
    ((again,),) = telegram(services, lambda f: user_text(ME, "/worker w2 reanudar"))
    assert "no está detenido" in again["text"]
    ((missing,),) = telegram(services, lambda f: user_text(ME, "/worker w9 reanudar"))
    assert "No existe el worker" in missing["text"]


def test_stopped_worker_is_skipped_until_resumed(env):
    build, root, conn = env
    services, claude = build(action=write_file("x.txt"))
    project = prepo.active_project(conn)
    prepo.set_worker_state(conn, project.id, "w1", "error")
    queue(conn, "t", "w1")
    assert run_once(services, "w1") is None  # detenido: no toma la tarea
    prepo.set_worker_state(conn, project.id, "w1", "idle")
    assert run_once(services, "w1").status == "done"


# --- etapa 5: eventos del cerebro, aprobación por Telegram, respuesta a preguntas -------------------------


def test_worker_events_wake_the_brain(env):
    from coclody.db import repo

    build, root, conn = env
    services, claude = build(action=write_file("x.txt"))
    pid = prepo.active_project(conn).id

    queue(conn, "una", "w1")
    run_once(services, "w1")  # done + cola vacía → worker_idle
    kinds = [e["kind"] for e in repo.pending_brain_events(conn, pid)]
    assert "worker_idle" in kinds

    # Una tarea que falla deja un evento task_failed.
    services.worker.runner = lambda inv, cancel: ClaudeResult(
        status="error", exit_code=1, session_id=None, result_text="roto", summary="roto",
        stdout_truncated=False, duration_s=0.1)
    queue(conn, "falla", "w2")
    run_once(services, "w2")
    assert "task_failed" in [e["kind"] for e in repo.pending_brain_events(conn, pid)]


def test_telegram_approval_flow(env, monkeypatch):
    from coclody.brain import service as brain_service
    from coclody.brain.schema import Decision
    from coclody.telegram_bot import app as app_module
    from telegram import Update
    from tests.fake_telegram import TOKEN, FakeTelegram, bot_message_with_buttons, callback_update, inline_data, user_text

    build, root, conn = env
    services, _ = build("brain_dry_run = false\nbrain_approval = true\n")
    pid = prepo.active_project(conn).id

    # Cerebro falso en modo aprobación: devuelve una decisión pendiente.
    def fake_run_once(*a, **k):
        with services.db() as c:
            run = repo.start_brain_run(c, pid, "t", "prompt", "{}", dry_run=False)
            d = Decision("enqueue", {"worker": "w1", "title": "revisá esto", "description": "d", "based_on": "r"}, "x")
            row = repo.add_brain_decision(c, run.id, 0, d.action, d.payload(), status="dry_run")
            repo.add_brain_approval(c, row.id)
        return brain_service.BrainOutcome(run.id, "ok", [d], None, "plan", mode="approval", pending=[(row.id, d)])

    monkeypatch.setattr(brain_service, "run_once", fake_run_once)

    fake = FakeTelegram()
    app = app_module.build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        await app.process_update(Update.de_json(user_text(ME, "/cerebro"), app.bot))
        # Entre los mensajes enviados, el de la decisión trae los botones ba:a / ba:r.
        idx, approval_msg = next((i, m) for i, m in enumerate(fake.sent()) if inline_data(m)
                                 and inline_data(m)[0].startswith("ba:"))
        data = inline_data(approval_msg)
        assert len(data) == 3 and data[0].startswith("ba:a:") and data[2].startswith("ba:c:")
        msg = bot_message_with_buttons(fake.returned_ids[idx], ME, approval_msg)
        before = len(fake.sent())
        await app.process_update(Update.de_json(callback_update(ME, data[0], message=msg), app.bot))
        await app.shutdown()
        return [m["text"] for m in fake.sent()[before:]]

    replies = asyncio.run(go())
    assert any("Aprobada" in t for t in replies)
    assert len([t for t in repo.list_tasks(conn) if t.title == "revisá esto"]) == 1  # recién ahora se creó


def test_telegram_reply_to_brain_question(env):
    from coclody.db import repo
    from coclody.telegram_bot import app as app_module
    from telegram import Update
    from tests.fake_telegram import TOKEN, FakeTelegram, bot_message, user_text

    build, root, conn = env
    services, _ = build("brain_dry_run = false\nbrain_approval = true\n")
    pid = prepo.active_project(conn).id
    q = repo.add_brain_question(conn, pid, "¿qué método de auth?", kind="open")
    repo.record_brain_question_delivery(conn, q.id, ME, "sent", 7777)

    fake = FakeTelegram()
    app = app_module.build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        reply_to = bot_message(7777, ME, "🧠❓ El cerebro necesita un dato")
        await app.process_update(Update.de_json(user_text(ME, "JWT", reply_to=reply_to), app.bot))
        await app.shutdown()
        return [m["text"] for m in fake.sent()]

    (reply,) = asyncio.run(go())
    assert "le pasé tu respuesta al cerebro" in reply.lower()
    q2 = repo.get_brain_question(conn, q.id)
    assert q2.status == "answered" and q2.answer == "JWT"
    assert "user_answer" in [e["kind"] for e in repo.pending_brain_events(conn, pid)]


def test_brain_loop_fires_on_events(env, monkeypatch):
    from coclody.brain import service as brain_service
    from coclody.db import repo
    from coclody.telegram_bot import app as app_module
    from tests.fake_telegram import TOKEN, FakeTelegram

    build, root, conn = env
    services, _ = build("brain_dry_run = true\n")  # simulacro: el loop solo avisa el plan
    pid = prepo.active_project(conn).id
    for name in ("BRAIN_POLL_S", "BRAIN_DEBOUNCE_S"):
        monkeypatch.setattr(app_module, name, 0.02)

    ran = []

    def fake_run_once(db, config, git, bd, hd, ec, trigger, event_ids=None):
        with services.db() as c:
            run = repo.start_brain_run(c, pid, trigger, "p", "{}", dry_run=True)
            if event_ids:
                repo.consume_brain_events(c, event_ids, run.id)
        ran.append(trigger)
        return brain_service.BrainOutcome(run.id, "ok", [], None, "🧠 plan", mode="dry_run")

    monkeypatch.setattr(brain_service, "run_once", fake_run_once)

    fake = FakeTelegram()
    app = app_module.build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        await app_module._post_init(app)
        with services.db() as c:
            repo.add_brain_event(c, pid, "worker_idle", None)
        for _ in range(200):
            if ran:
                break
            await asyncio.sleep(0.02)
        await app_module._stop_services(app)
        await app.shutdown()

    asyncio.run(go())
    assert ran and ran[0].startswith("eventos:")
    assert repo.pending_brain_events(conn, pid) == []  # el evento se consumió
