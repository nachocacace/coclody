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


def test_defaults_use_real_platform():
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
