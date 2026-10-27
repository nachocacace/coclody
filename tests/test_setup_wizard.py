from __future__ import annotations

import os
import stat
import tomllib

import pytest

from coclody.config import load_config
from coclody.credentials import SecretName, SecretStore
from coclody.setup_wizard import render_config, run_setup
from tests.test_credentials import TOKEN, MemoryKeyring

TG = SecretName.TELEGRAM_BOT_TOKEN


@pytest.fixture
def store():
    return SecretStore(backend=MemoryKeyring())


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "trabajo"
    ws.mkdir()
    return ws


class Script:
    """Respuestas programadas para ask/ask_secret, y captura de la salida."""

    def __init__(self, answers=(), secrets=()):
        self.answers = list(answers)
        self.secrets = list(secrets)
        self.lines: list[str] = []
        self.prompts: list[str] = []

    def ask(self, prompt):
        self.prompts.append(prompt)
        if not self.answers:
            raise EOFError
        return self.answers.pop(0)

    def ask_secret(self, prompt):
        self.prompts.append(prompt)
        if not self.secrets:
            raise EOFError
        return self.secrets.pop(0)

    def out(self, text):
        self.lines.append(text)

    @property
    def text(self):
        return "\n".join(self.lines)

    def run(self, paths, store):
        return run_setup(paths, store, ask=self.ask, ask_secret=self.ask_secret, out=self.out)


def test_happy_path(app_paths, store, workspace):
    s = Script(answers=["111", str(workspace)], secrets=[TOKEN])
    assert s.run(app_paths, store) == 0
    cfg = load_config(app_paths)
    assert cfg.telegram.allowed_chat_ids == frozenset({111})
    assert cfg.claude.workspace_root == workspace.resolve()
    assert store.get(TG) == TOKEN
    assert TOKEN not in s.text and TOKEN not in "".join(s.prompts)
    assert not app_paths.config_file.with_name("config.toml.tmp").exists()
    if os.name == "posix":
        assert stat.S_IMODE(app_paths.config_file.stat().st_mode) == 0o600


@pytest.mark.parametrize("bad", ["abc", "-5", "0", "12.5", "-100123456"])
def test_invalid_chat_id_is_asked_again(app_paths, store, workspace, bad):
    s = Script(answers=[bad, "111", str(workspace)], secrets=[TOKEN])
    assert s.run(app_paths, store) == 0
    assert "entero positivo" in s.text


def test_invalid_workspace_is_asked_again(app_paths, store, workspace, tmp_path):
    s = Script(
        answers=["111", str(tmp_path / "no-existe"), "relativa", str(app_paths.data_dir), str(workspace)],
        secrets=[TOKEN],
    )
    assert s.run(app_paths, store) == 0
    assert s.text.count("Esa configuración no es válida") == 3
    assert "la carpeta no existe" in s.text and "absoluta" in s.text and "carpeta de CoClody" in s.text


def test_workspace_with_tilde(app_paths, store, tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "proyectos").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    s = Script(answers=["111", "~/proyectos"], secrets=[TOKEN])
    assert s.run(app_paths, store) == 0
    assert load_config(app_paths).claude.workspace_root == (home / "proyectos").resolve()


def test_too_many_bad_workspaces(app_paths, store, tmp_path):
    s = Script(answers=["111"] + ["relativa"] * 5, secrets=[TOKEN])
    assert s.run(app_paths, store) == 1
    assert not app_paths.config_file.exists()
    assert not app_paths.config_file.with_name("config.toml.tmp").exists()


@pytest.mark.parametrize("answers", [[], ["111"], ["", "x"], ["111", ""]])
def test_abort_saves_nothing(app_paths, store, answers):
    s = Script(answers=answers)
    assert s.run(app_paths, store) == 1
    assert "cancelado" in s.text
    assert not app_paths.config_file.exists()
    assert not app_paths.config_file.with_name("config.toml.tmp").exists()
    assert store.get(TG) is None


def test_existing_config_is_not_touched(app_paths, store, workspace):
    original = render_config(222, str(workspace))
    app_paths.config_file.write_text(original, encoding="utf-8")
    s = Script(secrets=[TOKEN])
    assert s.run(app_paths, store) == 0
    assert app_paths.config_file.read_text(encoding="utf-8") == original
    assert "no lo modifico" in s.text and "es válida" in s.text


def test_existing_invalid_config_is_reported(app_paths, store):
    app_paths.config_file.write_text("[telegram]\nallowed_chat_ids = []\n", encoding="utf-8")
    s = Script(secrets=[TOKEN])
    assert s.run(app_paths, store) == 1
    assert "telegram.allowed_chat_ids" in s.text
    assert app_paths.config_file.read_text(encoding="utf-8") == "[telegram]\nallowed_chat_ids = []\n"


@pytest.mark.parametrize(("answer", "replaced"), [("", False), ("n", False), ("no", False), ("s", True), ("Sí", True)])
def test_existing_token(app_paths, store, workspace, answer, replaced):
    app_paths.config_file.write_text(render_config(111, str(workspace)), encoding="utf-8")
    store.set(TG, TOKEN)
    new = "987654321:BBH" + "y" * 32
    s = Script(answers=[answer], secrets=[new])
    assert s.run(app_paths, store) == 0
    assert store.get(TG) == (new if replaced else TOKEN)


def test_invalid_token_is_asked_again(app_paths, store, workspace):
    app_paths.config_file.write_text(render_config(111, str(workspace)), encoding="utf-8")
    s = Script(secrets=["basura", "123:corto", TOKEN])
    assert s.run(app_paths, store) == 0
    assert s.text.count("no tiene el formato esperado") == 2
    assert "basura" not in s.text


@pytest.mark.parametrize(
    "workspace_text",
    [r"C:\Users\Nacho\Mis proyectos", '/tmp/con "comillas"', "/tmp/acentos ñandú 🐢", "/tmp/a\\b\n[telegram]",
     "/tmp/del\x7fy\x00tab\t", "/tmp/\\u0041"],
)
def test_render_config_escapes_paths(workspace_text):
    data = tomllib.loads(render_config(42, workspace_text))
    assert data["claude"]["workspace_root"] == workspace_text
    assert data["telegram"]["allowed_chat_ids"] == [42]
