from __future__ import annotations

import sys

import pytest

from coclody import cli
from coclody.credentials import SecretName, SecretStore
from coclody.db import connect, migrate
from coclody.paths import AppPaths
from coclody.setup_wizard import render_config
from tests.test_credentials import TOKEN, MemoryKeyring


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    return root


@pytest.fixture
def exe(tmp_path, monkeypatch):
    path = tmp_path / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    path.parent.mkdir()
    path.write_bytes(b"")
    path.chmod(0o755)
    monkeypatch.setenv("PATH", str(path.parent))
    return path


@pytest.fixture
def store():
    return SecretStore(backend=MemoryKeyring())


def run_check(paths, store):
    lines = []
    code = cli.check(paths, store, out=lines.append)
    return code, "\n".join(lines)


def test_check_all_ok(app_paths, store, ws, exe):
    app_paths.config_file.write_text(render_config(1, str(ws)), encoding="utf-8")
    store.set(SecretName.TELEGRAM_BOT_TOKEN, TOKEN)
    code, text = run_check(app_paths, store)
    assert code == 0, text
    assert "[ERROR]" not in text and "Token del bot presente" in text and str(exe.resolve()) in text
    assert TOKEN not in text
    assert not app_paths.db_file.exists()  # check no crea ni migra nada


def test_check_reports_each_problem(app_paths, store, ws, monkeypatch, tmp_path):
    monkeypatch.setenv("PATH", str(tmp_path / "vacío"))
    app_paths.config_file.write_text(render_config(1, str(ws)), encoding="utf-8")
    code, text = run_check(app_paths, store)
    assert code == 2
    assert "Falta telegram_bot_token" in text and "[ERROR] Claude Code" in text


def test_check_invalid_config(app_paths, store):
    code, text = run_check(app_paths, store)
    assert code == 2 and "no existe" in text


def test_check_newer_db(app_paths, store, ws, exe):
    app_paths.config_file.write_text(render_config(1, str(ws)), encoding="utf-8")
    store.set(SecretName.TELEGRAM_BOT_TOKEN, TOKEN)
    conn = connect(app_paths.db_file)
    migrate(conn)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    code, text = run_check(app_paths, store)
    assert code == 2 and "versión más nueva" in text


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    root = tmp_path / "data"
    monkeypatch.setenv("COCLODY_DATA_DIR", str(root))
    calls = []
    monkeypatch.setattr("coclody.setup_wizard.run_setup", lambda paths, store: calls.append("setup") or 0)
    monkeypatch.setattr("coclody.main.run", lambda: calls.append("run") or 0)
    monkeypatch.setattr(cli, "SecretStore", lambda: SecretStore(backend=MemoryKeyring()))
    return AppPaths.under(root.resolve()), calls


def test_no_command_without_config_runs_setup(isolated):
    paths, calls = isolated
    assert cli.main([]) == 0 and calls == ["setup"]


def test_no_command_with_config_runs_bot(isolated):
    paths, calls = isolated
    paths.ensure()
    paths.config_file.write_text("", encoding="utf-8")
    assert cli.main([]) == 0 and calls == ["run"]


@pytest.mark.parametrize(("argv", "expected"), [(["setup"], ["setup"]), (["run"], ["run"])])
def test_explicit_commands(isolated, argv, expected):
    _, calls = isolated
    assert cli.main(argv) == 0 and calls == expected


def test_check_command(isolated, capsys):
    assert cli.main(["check"]) == 2
    assert "no existe" in capsys.readouterr().out


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0 and "CoClody" in capsys.readouterr().out


def test_invalid_data_dir(monkeypatch, capsys):
    monkeypatch.setenv("COCLODY_DATA_DIR", "relativa")
    assert cli.main(["check"]) == 2
    assert "COCLODY_DATA_DIR" in capsys.readouterr().err


def test_keyboard_interrupt(isolated, monkeypatch, capsys):
    def interrupted(paths, store):
        raise KeyboardInterrupt

    monkeypatch.setattr("coclody.setup_wizard.run_setup", interrupted)
    assert cli.main(["setup"]) == 130


def test_pause_only_for_frozen_windows_double_click(monkeypatch):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    assert cli._finish(3, bare=True) == 3 and asked == []  # desde fuente: nunca pausa
    monkeypatch.setattr(cli, "is_frozen", lambda: True)
    monkeypatch.setattr(sys, "platform", "win32")
    assert cli._finish(3, bare=False) == 3 and asked == []
    assert cli._finish(3, bare=True) == 3 and len(asked) == 1


# --- update ---------------------------------------------------------------------------


class StubUpdater:
    def __init__(self, status, message="msg"):
        from coclody.updater.client import UpdateResult

        self.result = UpdateResult(status, message, "0.2.0")

    def check(self):
        return self.result


@pytest.mark.parametrize(
    ("status", "code"),
    [("up_to_date", 0), ("available", 0), ("installed", 0), ("offline", 1), ("error", 2), ("disabled", 2)],
)
def test_update_exit_codes(app_paths, ws, status, code):
    app_paths.config_file.write_text(
        render_config(1, str(ws)).replace('# github_repo = ""', 'github_repo = "nacho/coclody"'), encoding="utf-8"
    )
    lines = []
    seen = []

    def factory(repo, updates_dir):
        seen.append((repo, updates_dir))
        return StubUpdater(status, f"resultado {status}")

    assert cli.update(app_paths, out=lines.append, updater_factory=factory) == code
    assert lines == [f"resultado {status}"]
    assert seen == [("nacho/coclody", app_paths.updates_dir)]


def test_update_needs_repo(app_paths, ws):
    app_paths.config_file.write_text(render_config(1, str(ws)), encoding="utf-8")
    lines = []
    assert cli.update(app_paths, out=lines.append) == 2
    assert "github_repo" in lines[0]


@pytest.mark.parametrize(("key", "line"), [(None, "[aviso] Sin clave pública de releases"),
                                           (object(), "[ok] Clave pública de releases configurada")])
def test_check_reports_release_key(app_paths, store, ws, exe, monkeypatch, key, line):
    monkeypatch.setattr("coclody.updater.pubkey.release_verify_key", lambda: key)
    app_paths.config_file.write_text(render_config(1, str(ws)), encoding="utf-8")
    store.set(SecretName.TELEGRAM_BOT_TOKEN, TOKEN)
    code, text = run_check(app_paths, store)
    assert code == 0 and line in text
