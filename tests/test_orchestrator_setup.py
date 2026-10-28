"""Etapa 1 del orquestador: config, git sin hooks ni red, worktrees, proyectos, CLI y Telegram.

Todo con git real sobre repos temporales. Nada de esto ejecuta todavía tareas en los worktrees:
eso es la etapa 2. Lo que se verifica acá es la base de las garantías por construcción.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys

import pytest

from coclody import cli
from coclody.config import ConfigError, command_problem, load_config
from coclody.db import connect, connection_factory, migrate, repo
from coclody.db import projects as prepo
from coclody.db.migrations import MIGRATIONS, _split
from coclody.db.repo import ValidationError
from coclody.orchestrator import projects as project_ops
from coclody.orchestrator.gitops import Git, GitError
from coclody.orchestrator.worktrees import (
    EXCLUDE_LINE,
    WorktreeError,
    ensure_worktrees,
    validate_root,
    worker_specs,
)
from tests.gitrepo import GIT, make_git, make_repo, needs_git, sh_git

posix_only = pytest.mark.skipif(os.name != "posix", reason="symlinks y hooks ejecutables sin privilegios: solo POSIX")
pytestmark = needs_git


def toml_path(path) -> str:
    return str(path).replace("\\", "\\\\")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root.resolve()


@pytest.fixture
def git(app_paths):
    return make_git(app_paths.git_hooks_dir)


@pytest.fixture
def cfg(app_paths, ws, monkeypatch):
    # Estos tests arman configs que pueden traer worker_commands; que se acepten corra donde corra el CI
    # (el rechazo sin sandbox lo cubre test_hardening.test_worker_commands_rejected_without_sandbox).
    monkeypatch.setattr("coclody.config._sandbox_platform", lambda: True)

    def write(extra: str = "enabled = true\n") -> object:
        app_paths.config_file.write_text(
            f'[telegram]\nallowed_chat_ids = [111]\n\n[claude]\nworkspace_root = "{toml_path(ws)}"\n\n'
            f"[orquestador]\n{extra}",
            encoding="utf-8",
        )
        return load_config(app_paths)

    return write


@pytest.fixture
def db(conn, app_paths):
    return connection_factory(app_paths.db_file)


# --- config ------------------------------------------------------------------------------------------


def test_orchestrator_defaults_and_off_by_default(app_paths, ws, cfg):
    app_paths.config_file.write_text(
        f'[telegram]\nallowed_chat_ids = [111]\n[claude]\nworkspace_root = "{toml_path(ws)}"\n', encoding="utf-8"
    )
    o = load_config(app_paths).orchestrator
    assert (o.enabled, o.workers, o.worker_commands, o.brain_dry_run) == (False, 3, (), True)
    assert (o.brain_calls_per_hour, o.brain_calls_per_day, o.autonomous_tasks_per_day, o.max_queue_per_worker) == (
        6, 40, 20, 3)
    assert (o.big_change_files, o.big_change_lines, o.audit_retention_days, o.web_enabled) == (20, 1000, 90, False)
    o = cfg('enabled = true\nworkers = 4\nworker_commands = ["pytest", "npm test"]\nweb_port = 9000\n').orchestrator
    assert (o.enabled, o.workers, o.worker_commands, o.web_port) == (True, 4, ("pytest", "npm test"), 9000)


@pytest.mark.parametrize("extra", ["workers = 0", "workers = 9", "brain_calls_per_hour = 0", "web_port = 80",
                                   "brain_dry_run = 1", "inventada = 1", 'worker_commands = "pytest"',
                                   'worker_commands = ["pytest", "pytest"]'])
def test_orchestrator_config_is_strict(cfg, extra):
    with pytest.raises(ConfigError):
        cfg(f"enabled = true\n{extra}\n")


@pytest.mark.parametrize("command", ["pytest", "python -m pytest", "python3 -m pytest -q", "npm test",
                                     "npm run build", "ruff check .", "cargo test", "go test ./...", "make test",
                                     "node scripts/check.js"])
def test_allowed_worker_commands(command):
    assert command_problem(command) is None


@pytest.mark.parametrize("command", ["git status", "git", "/usr/bin/git commit", "gh pr create", "bash -c x", "sh",
                                     "powershell", "env pytest", "sudo make", "curl http", "python", "python -c 1",
                                     "node -e 1", "python -m pip install x", "python -m http.server",
                                     "pytest; rm -rf /", "npm test && git push", "pytest | tee x", "pytest > out",
                                     "pytest $(whoami)", "pytest `id`", " pytest", "pytest  -q", "rm -rf build",
                                     "xargs git", 'pytest "x"', "pytest\nx"])
def test_rejected_worker_commands(command):
    assert command_problem(command) is not None


# --- gitops: sin hooks, sin red, sin GIT_* heredadas ---------------------------------------------


@posix_only
def test_hooks_never_run_even_if_the_repo_configures_them(ws, git, tmp_path):
    root = make_repo(ws / "proj")
    marker = tmp_path / "hook-corrio"
    hooks = tmp_path / "hooks-del-repo"
    hooks.mkdir()
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch '{marker}'\n", encoding="utf-8")
    hook.chmod(0o755)
    sh_git(root, "config", "core.hooksPath", str(hooks))
    # Control: con git directo el hook sí corre.
    sh_git(root, "worktree", "add", "-q", "-b", "control", str(tmp_path / "control"))
    assert marker.exists()
    marker.unlink()
    git.add_worktree(root, root / "wt", "coclody/w1", "main")
    assert (root / "wt" / "README.md").exists() and not marker.exists()


def test_git_refuses_to_run_if_the_empty_hooks_dir_has_files(ws, git, app_paths):
    root = make_repo(ws / "proj")
    app_paths.git_hooks_dir.mkdir()
    (app_paths.git_hooks_dir / "post-checkout").write_text("x", encoding="utf-8")
    with pytest.raises(GitError, match="hooks"):
        git.toplevel(root)


def test_git_argv_disables_hooks_network_fsmonitor_and_pager(ws, git, monkeypatch):
    root = make_repo(ws / "proj")
    seen = {}
    real_run = subprocess.run

    def spy(argv, **kwargs):
        seen["argv"], seen["env"], seen["shell"] = argv, kwargs["env"], kwargs.get("shell", False)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    monkeypatch.setenv("GIT_DIR", str(ws / "otro"))
    monkeypatch.setenv("GIT_WORK_TREE", str(ws))
    assert git.toplevel(root) == root  # GIT_DIR/GIT_WORK_TREE heredadas no lo desvían
    argv = seen["argv"]
    assert f"core.hooksPath={git.hooks_dir}" in argv and "protocol.allow=never" in argv
    assert "core.fsmonitor=false" in argv and "--no-pager" in argv and seen["shell"] is False
    assert not any(k.upper().startswith("GIT_") and k not in ("GIT_TERMINAL_PROMPT", "GIT_OPTIONAL_LOCKS")
                   for k in seen["env"])


@pytest.mark.parametrize("branch", ["-x", "a..b", "con espacio", "", "fin/"])
def test_bad_branch_names_are_rejected(ws, git, branch):
    root = make_repo(ws / "proj")
    with pytest.raises(GitError):
        git.add_worktree(root, root / "wt", branch, "main")
    assert not (root / "wt").exists()


def test_git_inside_the_workspace_is_never_used(ws, app_paths, monkeypatch):
    fake = ws / "bin" / ("git.exe" if sys.platform == "win32" else "git")
    fake.parent.mkdir()
    fake.write_bytes(b"#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake.parent))
    with pytest.raises(GitError, match="dentro de la carpeta de trabajo"):
        Git.find(ws, app_paths.git_hooks_dir)


# --- raíz del proyecto ---------------------------------------------------------------------------------


def test_valid_root_returns_its_branch(ws, git):
    root = make_repo(ws / "proj", branch="develop")
    assert validate_root(git, root, ws) == (root, "develop")


def test_root_problems(ws, git, tmp_path):
    outside = make_repo(tmp_path / "afuera")
    with pytest.raises(WorktreeError, match="dentro de la carpeta de trabajo"):
        validate_root(git, outside, ws)
    root = make_repo(ws / "proj")
    (root / "sub").mkdir()
    with pytest.raises(WorktreeError, match="raíz del repo"):
        validate_root(git, root / "sub", ws)
    (ws / "suelta").mkdir()
    with pytest.raises(WorktreeError, match="no es un repo"):
        validate_root(git, ws / "suelta", ws)
    with pytest.raises(WorktreeError, match="no existe"):
        validate_root(git, ws / "nada", ws)
    sh_git(root, "worktree", "add", "-q", "-b", "otra", str(ws / "secundario"))
    with pytest.raises(WorktreeError, match="secundario"):
        validate_root(git, ws / "secundario", ws)
    sh_git(root, "checkout", "-q", "--detach")
    with pytest.raises(WorktreeError, match="detached"):
        validate_root(git, root, ws)
    sh_git(root, "checkout", "-q", "-b", "coclody/w1")
    with pytest.raises(WorktreeError, match="rama de worker"):
        validate_root(git, root, ws)


# --- worktrees -------------------------------------------------------------------------------------------


def test_creates_missing_worktrees_inside_the_project(ws, git):
    root = make_repo(ws / "proj")
    reports = ensure_worktrees(git, root, "main", worker_specs(3), create=True)
    assert [(r.spec.name, r.status) for r in reports] == [("w1", "created"), ("w2", "created"), ("w3", "created")]
    for i in (1, 2, 3):
        wt = root / ".coclody" / "worktrees" / f"w{i}"
        assert (wt / "README.md").exists()
        assert sh_git(wt, "rev-parse", "--abbrev-ref", "HEAD").strip() == f"coclody/w{i}"
        assert wt.resolve().is_relative_to(root)
    assert sh_git(root, "status", "--porcelain") == ""  # .coclody/ excluido sin tocar archivos versionados
    assert sh_git(root, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"  # la raíz no cambió de rama

    again = ensure_worktrees(git, root, "main", worker_specs(3), create=True)
    assert [r.status for r in again] == ["ok"] * 3
    exclude = (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")
    assert exclude.splitlines().count(EXCLUDE_LINE) == 1


def test_existing_worker_branch_is_reused_not_recreated(ws, git):
    root = make_repo(ws / "proj")
    sh_git(root, "branch", "coclody/w1")
    (root / "otro.txt").write_text("x", encoding="utf-8")
    sh_git(root, "add", "otro.txt")
    sh_git(root, "commit", "-q", "-m", "después")
    (report,) = ensure_worktrees(git, root, "main", worker_specs(1), create=True)
    assert report.status == "created"
    assert not (root / ".coclody" / "worktrees" / "w1" / "otro.txt").exists()  # quedó en su rama, no en main


def test_check_mode_creates_nothing(ws, git):
    root = make_repo(ws / "proj")
    reports = ensure_worktrees(git, root, "main", worker_specs(2), create=False)
    assert [r.status for r in reports] == ["missing", "missing"]
    assert not (root / ".coclody").exists()
    assert EXCLUDE_LINE not in (root / ".git" / "info" / "exclude").read_text(encoding="utf-8")


def test_existing_folder_that_is_not_a_worktree_is_left_alone(ws, git):
    root = make_repo(ws / "proj")
    impostor = root / ".coclody" / "worktrees" / "w1"
    impostor.mkdir(parents=True)
    (impostor / "mio.txt").write_text("no tocar", encoding="utf-8")
    (report,) = ensure_worktrees(git, root, "main", worker_specs(1), create=True)
    assert report.status == "error" and "no es un worktree" in report.problem
    assert (impostor / "mio.txt").read_text(encoding="utf-8") == "no tocar"


def test_worktree_on_the_wrong_branch_is_an_error(ws, git):
    root = make_repo(ws / "proj")
    ensure_worktrees(git, root, "main", worker_specs(1), create=True)
    sh_git(root / ".coclody" / "worktrees" / "w1", "checkout", "-q", "-b", "desvio")
    (report,) = ensure_worktrees(git, root, "main", worker_specs(1), create=True)
    assert report.status == "error" and "desvio" in report.problem


def test_worker_branch_equal_to_base_is_protected(ws, git):
    root = make_repo(ws / "proj")
    sh_git(root, "branch", "coclody/w2")  # una base con nombre de rama de worker (validate_root la rechaza antes)
    reports = ensure_worktrees(git, root, "coclody/w2", worker_specs(2), create=True)
    assert [r.status for r in reports] == ["created", "error"]
    assert "protegida" in reports[1].problem


@posix_only
def test_symlinked_container_never_creates_anything_outside(ws, git, tmp_path):
    root = make_repo(ws / "proj")
    outside = tmp_path / "afuera"
    outside.mkdir()
    (root / ".coclody").symlink_to(outside, target_is_directory=True)
    reports = ensure_worktrees(git, root, "main", worker_specs(2), create=True)
    assert all(r.status == "error" and "symlink" in r.problem for r in reports)
    assert list(outside.iterdir()) == []


# --- proyectos ---------------------------------------------------------------------------------------------


def test_add_and_activate_project(ws, git, cfg, db, conn):
    config = cfg()
    root = make_repo(ws / "proj")
    project = project_ops.add(db, git, config, "web", root)
    assert (project.name, project.root, project.base_branch, project.active) == ("web", str(root), "main", 0)
    activation = project_ops.activate(db, git, config, "web")
    assert activation.project.active == 1 and [r.status for r in activation.reports] == ["created"] * 3
    assert [(w.name, w.branch, w.path_rel) for w in prepo.list_workers(conn, project.id)] == [
        ("w1", "coclody/w1", ".coclody/worktrees/w1"), ("w2", "coclody/w2", ".coclody/worktrees/w2"),
        ("w3", "coclody/w3", ".coclody/worktrees/w3")]


@pytest.mark.parametrize("name", ["", "Web", "con espacio", "-x", "a" * 33, "../x"])
def test_project_names_are_validated(ws, git, cfg, db, name):
    with pytest.raises(project_ops.ProjectError):
        project_ops.add(db, git, cfg(), name, make_repo(ws / "proj"))


def test_duplicate_name_or_root_is_rejected(ws, git, cfg, db):
    config = cfg()
    root = make_repo(ws / "proj")
    project_ops.add(db, git, config, "web", root)
    with pytest.raises(project_ops.ProjectError, match="ya existe"):
        project_ops.add(db, git, config, "web", make_repo(ws / "otro"))
    with pytest.raises(project_ops.ProjectError, match="carpeta raíz"):
        project_ops.add(db, git, config, "web2", root)


def test_switching_projects_changes_the_single_active_one(ws, git, cfg, db, conn):
    config = cfg()
    project_ops.add(db, git, config, "uno", make_repo(ws / "uno"))
    project_ops.add(db, git, config, "dos", make_repo(ws / "dos"))
    project_ops.activate(db, git, config, "uno")
    project_ops.activate(db, git, config, "dos")
    assert [p.name for p in prepo.list_projects(conn) if p.active] == ["dos"]
    assert prepo.active_project(conn).name == "dos"


def test_cannot_switch_project_with_running_tasks(ws, git, cfg, db, conn):
    config = cfg()
    project_ops.add(db, git, config, "uno", make_repo(ws / "uno"))
    project_ops.add(db, git, config, "dos", make_repo(ws / "dos"))
    project_ops.activate(db, git, config, "uno")
    t = repo.create_task(conn, "larga")
    repo.transition_task(conn, t.id, "queued")
    repo.transition_task(conn, t.id, "running")
    with pytest.raises(project_ops.ProjectError, match="ejecutándose"):
        project_ops.activate(db, git, config, "dos")
    with pytest.raises(prepo.ProjectBusyError):  # también en la base, por si dos caminos compiten
        prepo.activate_project(conn, "dos")
    assert prepo.active_project(conn).name == "uno"
    assert not (ws / "dos" / ".coclody").exists()  # ni siquiera se crearon sus worktrees


def test_bad_worktree_keeps_the_previous_project_active(ws, git, cfg, db, conn):
    config = cfg()
    project_ops.add(db, git, config, "uno", make_repo(ws / "uno"))
    dos = make_repo(ws / "dos")
    project_ops.add(db, git, config, "dos", dos)
    project_ops.activate(db, git, config, "uno")
    (dos / ".coclody" / "worktrees" / "w2").mkdir(parents=True)  # carpeta que no es worktree
    with pytest.raises(project_ops.ProjectError) as exc:
        project_ops.activate(db, git, config, "dos")
    assert any(r.spec.name == "w2" and r.status == "error" for r in exc.value.reports)
    assert prepo.active_project(conn).name == "uno"


def test_root_on_another_branch_than_registered_is_refused(ws, git, cfg, db, conn):
    config = cfg()
    root = make_repo(ws / "proj")
    project_ops.add(db, git, config, "web", root)
    sh_git(root, "checkout", "-q", "-b", "experimento")
    with pytest.raises(project_ops.ProjectError, match="experimento"):
        project_ops.activate(db, git, config, "web")
    assert prepo.active_project(conn) is None


def test_fewer_workers_never_deletes_a_worktree(ws, git, cfg, db, conn):
    root = make_repo(ws / "proj")
    project = project_ops.add(db, git, cfg(), "web", root)
    project_ops.activate(db, git, cfg(), "web")
    project_ops.activate(db, git, cfg("enabled = true\nworkers = 2\n"), "web")
    assert [w.name for w in prepo.list_workers(conn, project.id)] == ["w1", "w2"]
    assert (root / ".coclody" / "worktrees" / "w3" / "README.md").exists()
    assert "w3" in sh_git(root, "worktree", "list")


# --- base: migración v6 y compatibilidad con el modo de un worker -------------------------------------------


def test_migration_v6_keeps_v5_data(app_paths):
    c = connect(app_paths.db_file)
    for script in MIGRATIONS[:5]:
        for statement in _split(script):
            c.execute(statement)
    c.execute("PRAGMA user_version = 5")
    c.execute("INSERT INTO tasks (title, status) VALUES ('vieja', 'queued')")
    migrate(c)
    task = repo.get_task(c, 1)
    assert (task.title, task.project_id, task.worker, task.origin, task.brain_decision_id) == (
        "vieja", None, None, "user", None)
    assert repo.next_queued_task(c).id == 1  # el modo de un worker la sigue tomando
    c.close()


def test_single_worker_mode_never_takes_a_worker_task(conn):
    t = repo.create_task(conn, "de w1")
    repo.transition_task(conn, t.id, "queued")
    conn.execute("UPDATE tasks SET worker = 'w1' WHERE id = ?", (t.id,))
    assert repo.next_queued_task(conn) is None


def test_only_one_active_project_in_the_db(conn):
    conn.execute("INSERT INTO projects (name, root, base_branch, active) VALUES ('a', '/a', 'main', 1)")
    with pytest.raises(Exception):
        conn.execute("INSERT INTO projects (name, root, base_branch, active) VALUES ('b', '/b', 'main', 1)")


def test_worker_names_are_validated(conn):
    conn.execute("INSERT INTO projects (name, root, base_branch) VALUES ('a', '/a', 'main')")
    with pytest.raises(ValidationError):
        prepo.sync_workers(conn, 1, [("W1; drop", "coclody/w1", "x")])


# --- CLI -------------------------------------------------------------------------------------------------


def project_cli(app_paths, *argv):
    parser_args = argparse.Namespace(accion=argv[0], nombre=argv[1] if len(argv) > 1 else None,
                                     ruta=argv[2] if len(argv) > 2 else None, activar="--activar" in argv)
    lines = []
    code = cli.project_command(app_paths, parser_args, out=lines.append)
    return code, "\n".join(lines)


def test_cli_add_list_activate(ws, cfg, app_paths):
    cfg()
    root = make_repo(ws / "proj")
    code, text = project_cli(app_paths, "agregar", "web", str(root), "--activar")
    assert code == 0 and "Proyecto web registrado" in text and "Proyecto activo: web" in text
    assert "w1 (coclody/w1): creado" in text
    code, text = project_cli(app_paths, "listar")
    assert code == 0 and f"* web  {root}" in text
    code, text = project_cli(app_paths, "activar", "nada")
    assert code == 2 and "no existe el proyecto" in text


def test_cli_rejects_a_root_outside_the_workspace(ws, cfg, app_paths, tmp_path):
    cfg()
    code, text = project_cli(app_paths, "agregar", "web", str(make_repo(tmp_path / "afuera")))
    assert code == 2 and "dentro de la carpeta de trabajo" in text


def test_check_reports_the_active_project_without_creating_anything(ws, cfg, app_paths, monkeypatch):
    from tests.test_cli import run_check

    cfg()
    root = make_repo(ws / "proj")
    project_cli(app_paths, "agregar", "web", str(root))
    with connection_factory(app_paths.db_file)() as c:
        prepo.activate_project(c, "web")  # activo pero sin worktrees todavía

    class Store:
        def get(self, name):
            return "123:abc"

    monkeypatch.setattr(cli, "resolve_executable", lambda *a: ws)
    _, text = run_check(app_paths, Store())
    assert "Orquestador (modo v2): activado, 3 workers" in text and "Proyecto activo: web" in text
    assert "[aviso] Worktree w1 (coclody/w1): no existe" in text
    assert not (root / ".coclody").exists()


# --- Telegram: /proyectos y /proyecto -------------------------------------------------------------------------


def telegram(services, text):
    from telegram import Update

    from coclody.telegram_bot.app import build_application
    from tests.fake_telegram import TOKEN, FakeTelegram, user_text

    fake = FakeTelegram()
    app = build_application(TOKEN, services, request=fake, get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        await app.process_update(Update.de_json(user_text(111, text), app.bot))
        await app.shutdown()

    asyncio.run(go())
    return [p["text"] for p in fake.sent()]


@pytest.fixture
def services(cfg, db, git, app_paths, ws):
    from coclody.orchestrator.worker import Worker
    from coclody.security import ChatGuard, close_security_log, configure_security_log
    from coclody.telegram_bot.app import Services

    configure_security_log(app_paths.security_log)

    def make(extra="enabled = true\n"):
        config = cfg(extra)
        return Services(config=config, db=db, worker=Worker(config.claude, db), guard=ChatGuard({111}), git=git, app_paths=app_paths)

    yield make
    close_security_log()


def test_telegram_project_commands_need_the_orchestrator(services):
    off = services("enabled = false\n")
    assert "no está activado" in telegram(off, "/proyectos")[0]
    assert "no está activado" in telegram(off, "/proyecto web")[0]


def test_telegram_lists_and_switches_projects(services, ws, git, db, conn):
    s = services()
    assert "No hay proyectos" in telegram(s, "/proyectos")[0]
    project_ops.add(db, git, s.config, "uno", make_repo(ws / "uno"))
    project_ops.add(db, git, s.config, "dos", make_repo(ws / "dos"))
    (reply,) = telegram(s, "/proyecto dos")
    assert reply.startswith("Proyecto activo: dos") and "w1, w2, w3" in reply
    (listing,) = telegram(s, "/proyectos")
    assert "▶ dos (activo)" in listing and "• uno" in listing
    assert str(ws) not in listing  # nombres, no rutas
    assert "Uso: /proyecto" in telegram(s, "/proyecto")[0]
    assert "no existe el proyecto" in telegram(s, "/proyecto nada")[0]


def test_telegram_refuses_to_switch_with_running_tasks(services, ws, git, db, conn):
    s = services()
    project_ops.add(db, git, s.config, "uno", make_repo(ws / "uno"))
    project_ops.add(db, git, s.config, "dos", make_repo(ws / "dos"))
    project_ops.activate(db, git, s.config, "uno")
    t = repo.create_task(conn, "larga")
    repo.transition_task(conn, t.id, "queued")
    repo.transition_task(conn, t.id, "running")
    (reply,) = telegram(s, "/proyecto dos")
    assert reply.startswith("No cambié de proyecto") and "#1" in reply
    assert prepo.active_project(conn).name == "uno"


def test_menu_includes_project_commands_only_with_the_orchestrator():
    from telegram import Bot

    from coclody.telegram_bot.app import setup_menu
    from tests.fake_telegram import TOKEN, FakeTelegram

    def commands(orchestrator):
        fake = FakeTelegram()

        async def go():
            async with Bot(TOKEN, request=fake) as bot:
                await setup_menu(bot, {111}, orchestrator)

        asyncio.run(go())
        return [c["command"] for c in fake.called("setMyCommands")[0]["commands"]]

    assert "proyecto" not in commands(False)
    assert {"proyecto", "proyectos"} <= set(commands(True))


def test_git_found_for_tests():
    assert GIT is not None


# --- reglas y contexto por Telegram y CLI (etapa 3) ------------------------------------------------------


def active(services, db, git, ws):
    """Crea y activa un proyecto 'web' para los tests de reglas/contexto."""
    s = services()
    project_ops.add(db, git, s.config, "web", make_repo(ws / "web"))
    project_ops.activate(db, git, s.config, "web")
    return s


def test_rules_commands_need_the_orchestrator(services):
    off = services("enabled = false\n")
    assert "no está activado" in telegram(off, "/reglas")[0]
    assert "no está activado" in telegram(off, "/contexto")[0]


def test_telegram_rules_crud(services, db, git, ws, conn):
    s = active(services, db, git, ws)
    assert "no tiene reglas" in telegram(s, "/reglas")[0]
    assert "Agregué la regla #1" in telegram(s, "/reglas agregar Usá pytest")[0]
    (listing,) = telegram(s, "/reglas")
    assert "🟢 #1 Usá pytest" in listing
    assert "desactivada" in telegram(s, "/reglas off 1")[0]
    assert "⚪ #1" in telegram(s, "/reglas")[0]
    assert "Borré la regla #1" in telegram(s, "/reglas quitar 1")[0]
    from coclody.db import knowledge
    assert knowledge.list_rules(conn, prepo.active_project(conn).id) == []


def test_telegram_user_context_is_approved_and_listed(services, db, git, ws, conn):
    s = active(services, db, git, ws)
    assert "Agregué el contexto #1 (aprobado)" in telegram(s, "/contexto agregar El deploy es manual")[0]
    (listing,) = telegram(s, "/contexto")
    assert "Aprobado:" in listing and "#1 El deploy es manual" in listing


def test_telegram_shows_brain_drafts_with_buttons_and_approves(services, db, git, ws, conn):
    from telegram import Update

    from coclody.db import knowledge
    from coclody.telegram_bot.app import build_application
    from tests.fake_telegram import TOKEN, FakeTelegram, bot_message_with_buttons, callback_update, inline_data, user_text

    s = active(services, db, git, ws)
    pid = prepo.active_project(conn).id
    draft = knowledge.propose_context(conn, pid, "Parece que usan Postgres")

    fake = FakeTelegram()
    app = build_application(TOKEN, s, request=fake, get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        await app.process_update(Update.de_json(user_text(111, "/contexto"), app.bot))
        # El último mensaje es el borrador con botones Aprobar/Rechazar.
        draft_msg = fake.sent()[-1]
        assert inline_data(draft_msg) == [f"cx:a:{draft.id}", f"cx:r:{draft.id}"]
        msg = bot_message_with_buttons(fake.returned_ids[-1], 111, draft_msg)
        before = len(fake.sent())
        await app.process_update(Update.de_json(callback_update(111, f"cx:a:{draft.id}", message=msg), app.bot))
        await app.shutdown()
        return [m["text"] for m in fake.sent()[before:]]

    replies = asyncio.run(go())
    assert any("aprobado" in t for t in replies)
    assert "Postgres" in knowledge.approved_context_text(conn, pid)


def test_telegram_reject_draft(services, db, git, ws, conn):
    from telegram import Update

    from coclody.db import knowledge
    from coclody.telegram_bot.app import build_application
    from tests.fake_telegram import TOKEN, FakeTelegram, bot_message, callback_update

    s = active(services, db, git, ws)
    pid = prepo.active_project(conn).id
    draft = knowledge.propose_context(conn, pid, "dato dudoso")

    fake = FakeTelegram()
    app = build_application(TOKEN, s, request=fake, get_updates_request=FakeTelegram())

    async def go():
        await app.initialize()
        msg = bot_message(500, 111, "Borrador")
        await app.process_update(Update.de_json(callback_update(111, f"cx:r:{draft.id}", message=msg), app.bot))
        await app.shutdown()
        return [m["text"] for m in fake.sent()]

    assert any("rechazado" in t for t in asyncio.run(go()))
    assert knowledge.list_context(conn, pid, statuses=("rejected",))[0].id == draft.id


def test_rules_menu_entries_only_with_orchestrator():
    from telegram import Bot

    from coclody.telegram_bot.app import setup_menu
    from tests.fake_telegram import TOKEN, FakeTelegram

    def commands(orchestrator):
        fake = FakeTelegram()

        async def go():
            async with Bot(TOKEN, request=fake) as bot:
                await setup_menu(bot, {111}, orchestrator)

        asyncio.run(go())
        return {c["command"] for c in fake.called("setMyCommands")[0]["commands"]}

    assert not {"reglas", "contexto"} & commands(False)
    assert {"reglas", "contexto"} <= commands(True)


def test_cli_rules_and_context(services, db, git, ws, app_paths, conn):
    active(services, db, git, ws)  # deja 'web' activo en la base
    lines = []
    assert cli.knowledge_command(app_paths, "reglas", argparse.Namespace(accion="agregar", texto=["Usá", "pytest"]),
                                 out=lines.append) == 0
    assert any("Agregué" in ln for ln in lines)
    lines.clear()
    cli.knowledge_command(app_paths, "reglas", argparse.Namespace(accion="listar"), out=lines.append)
    assert any("on  #1  Usá pytest" in ln for ln in lines)
    lines.clear()
    assert cli.knowledge_command(app_paths, "contexto", argparse.Namespace(accion="agregar", texto=["deploy", "manual"]),
                                 out=lines.append) == 0
    lines.clear()
    cli.knowledge_command(app_paths, "contexto", argparse.Namespace(accion="listar"), out=lines.append)
    assert any("approved #1  deploy manual" in ln for ln in lines)


def test_cerebro_command(services, db, git, ws, conn, monkeypatch):
    from coclody.brain import service as brain_service
    from coclody.brain.schema import Decision

    off = services("enabled = false\n")
    assert "no está activado" in telegram(off, "/cerebro")[0]

    s = active(services, db, git, ws)
    # Cerebro falso: no corre Claude, devuelve un plan de simulacro.
    outcome = brain_service.BrainOutcome(run_id=1, parse_status="ok",
                                         decisions=[Decision("wait", {}, "nada que hacer")],
                                         error=None, plan="🧠 Simulacro del cerebro (run #1)...")
    # run_brain_dry importa coclody.brain.service adentro, así que parcheamos ahí.
    monkeypatch.setattr(brain_service, "run_once", lambda *a, **k: outcome)
    (reply,) = telegram(s, "/cerebro")
    assert "Simulacro del cerebro" in reply
