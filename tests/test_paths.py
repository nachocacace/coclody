from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from coclody import paths
from coclody.paths import AppPaths, default_paths

HOME = Path("/home/tester")


def test_macos_conventions():
    p = default_paths(platform="darwin", env={}, home=HOME)
    assert p.data_dir == HOME / "Library" / "Application Support" / "CoClody"
    assert p.config_file == p.data_dir / "config.toml"
    assert p.db_file == p.data_dir / "coclody.db"
    assert p.logs_dir == HOME / "Library" / "Logs" / "CoClody"
    assert p.security_log == p.logs_dir / "security.log"


def test_windows_uses_localappdata():
    p = default_paths(platform="win32", env={"LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}, home=HOME)
    assert p.data_dir == Path(r"C:\Users\tester\AppData\Local") / "CoClody"
    assert p.logs_dir == p.data_dir / "logs"
    assert p.config_file == p.data_dir / "config.toml"


@pytest.mark.parametrize("value", ["", "   ", r"relative\dir"])
def test_windows_falls_back_without_valid_localappdata(value):
    p = default_paths(platform="win32", env={"LOCALAPPDATA": value}, home=HOME)
    assert p.data_dir == HOME / "AppData" / "Local" / "CoClody"


def test_windows_ignores_roaming_appdata():
    env = {"APPDATA": r"C:\Users\tester\AppData\Roaming", "LOCALAPPDATA": r"C:\Users\tester\AppData\Local"}
    assert "Roaming" not in str(default_paths(platform="win32", env=env, home=HOME).data_dir)


def test_linux_xdg_and_relative_values_ignored():
    env = {"XDG_DATA_HOME": "/xdg/data", "XDG_CONFIG_HOME": "relative", "XDG_STATE_HOME": "/xdg/state"}
    p = default_paths(platform="linux", env=env, home=HOME)
    assert p.data_dir == Path("/xdg/data/coclody")
    assert p.config_dir == HOME / ".config" / "coclody"
    assert p.logs_dir == Path("/xdg/state/coclody")


def test_defaults_use_real_platform(monkeypatch):
    monkeypatch.delenv("COCLODY_DATA_DIR", raising=False)
    assert default_paths() == default_paths(platform=sys.platform, env=os.environ, home=Path.home())


def test_does_not_depend_on_module_location(monkeypatch, tmp_path):
    # En PyInstaller __file__ apunta a una carpeta temporal; las rutas no deben cambiar.
    before = default_paths(platform="darwin", env={}, home=HOME)
    monkeypatch.setattr(paths, "__file__", str(tmp_path / "_MEI123" / "coclody" / "paths.py"))
    monkeypatch.chdir(tmp_path)
    assert default_paths(platform="darwin", env={}, home=HOME) == before


def test_ensure_creates_private_dirs(tmp_path):
    p = AppPaths.under(tmp_path / "root")
    p.ensure()
    for d in p.all_dirs():
        assert d.is_dir()
        if os.name == "posix":
            assert stat.S_IMODE(d.stat().st_mode) == 0o700
    p.ensure()  # idempotente


def test_frozen_detection(monkeypatch):
    assert paths.is_frozen() is False
    assert paths.executable_path() is None
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", "/tmp/_MEI123", raising=False)
    assert paths.is_frozen() is True
    assert paths.executable_path() == Path(sys.executable).resolve()


# --- COCLODY_DATA_DIR -----------------------------------------------------------


def test_data_dir_override_puts_everything_under_it(tmp_path):
    root = tmp_path / "coclody-test"
    for platform in ("darwin", "win32", "linux"):
        p = default_paths(platform=platform, env={"COCLODY_DATA_DIR": str(root)}, home=tmp_path / "home")
        assert p == AppPaths.under(root.resolve())
        assert p.db_file == root.resolve() / "coclody.db"
        assert p.security_log == root.resolve() / "logs" / "security.log"


def test_data_dir_override_inside_home_is_fine(tmp_path):
    home = tmp_path / "home"
    p = default_paths(platform="darwin", env={"COCLODY_DATA_DIR": str(home / "pruebas")}, home=home)
    assert p.data_dir == (home / "pruebas").resolve()


def test_data_dir_override_expands_user(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    p = default_paths(platform="darwin", env={"COCLODY_DATA_DIR": "~/x"}, home=tmp_path / "otro")
    assert p.data_dir == (tmp_path / "x").resolve()


def _anchor() -> str:
    return Path.cwd().anchor


@pytest.mark.parametrize(
    "make_value",
    [
        lambda home: "",
        lambda home: "   ",
        lambda home: "relativa/carpeta",
        lambda home: _anchor(),
        lambda home: str(home),
        lambda home: str(home) + os.sep,
        lambda home: str(home.parent),
        lambda home: str(home / "sub" / ".." / ".."),
    ],
)
def test_data_dir_override_rejects(tmp_path, make_value):
    home = tmp_path / "home"
    home.mkdir()
    with pytest.raises(paths.DataDirError):
        default_paths(platform=sys.platform, env={"COCLODY_DATA_DIR": make_value(home)}, home=home)


@pytest.mark.skipif(os.name != "posix", reason="symlinks sin privilegios solo en POSIX")
def test_data_dir_override_symlink_to_home_rejected(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    link = tmp_path / "link"
    link.symlink_to(home, target_is_directory=True)
    with pytest.raises(paths.DataDirError):
        default_paths(platform="darwin", env={"COCLODY_DATA_DIR": str(link)}, home=home)


def test_data_dir_override_gets_private_permissions(tmp_path):
    p = default_paths(platform=sys.platform, env={"COCLODY_DATA_DIR": str(tmp_path / "d")}, home=tmp_path / "h")
    p.ensure()
    if os.name == "posix":
        for d in p.all_dirs():
            assert stat.S_IMODE(d.stat().st_mode) == 0o700
