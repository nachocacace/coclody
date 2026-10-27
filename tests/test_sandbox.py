from __future__ import annotations

import os
import sys

import pytest

from coclody.orchestrator.sandbox import SandboxError, resolve_executable, resolve_workdir

posix_only = pytest.mark.skipif(os.name != "posix", reason="symlinks sin privilegios solo en POSIX")


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "proj" / "sub").mkdir(parents=True)
    (root / "archivo.txt").write_text("x")
    (tmp_path / "afuera").mkdir()
    return root


def make_exe(directory, name="claude"):
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + (".exe" if sys.platform == "win32" else ""))
    path.write_bytes(b"#!/bin/sh\n")
    path.chmod(0o755)
    return path


# --- carpeta de trabajo ---------------------------------------------------------


def test_root_and_subdirs(ws):
    assert resolve_workdir(ws, None) == ws.resolve()
    assert resolve_workdir(ws, "") == ws.resolve()
    assert resolve_workdir(ws, "proj") == (ws / "proj").resolve()
    assert resolve_workdir(ws, r"proj\sub") == (ws / "proj" / "sub").resolve()
    assert resolve_workdir(ws, "./proj/./sub/") == (ws / "proj" / "sub").resolve()


@pytest.mark.parametrize(
    "escape",
    [
        "..",
        "../afuera",
        "proj/../../afuera",
        r"proj\..\..\afuera",
        "/etc",
        "/",
        r"C:\Windows",
        "C:",
        r"\\servidor\recurso",
        "proj/sub:stream",
        "proj\x00",
    ],
)
def test_escapes_are_rejected(ws, escape):
    with pytest.raises(SandboxError):
        resolve_workdir(ws, escape)


def test_absolute_path_to_inside_is_still_rejected(ws):
    # Aunque apunte adentro, una ruta absoluta nunca se acepta como workdir_rel.
    with pytest.raises(SandboxError):
        resolve_workdir(ws, str(ws / "proj"))


def test_missing_and_non_dir(ws):
    with pytest.raises(SandboxError, match="no existe"):
        resolve_workdir(ws, "no-existe")
    with pytest.raises(SandboxError, match="no es una carpeta"):
        resolve_workdir(ws, "archivo.txt")
    with pytest.raises(SandboxError, match="no existe"):
        resolve_workdir(ws / "nada", None)


@posix_only
def test_symlink_pointing_outside_is_rejected(ws, tmp_path):
    (ws / "atajo").symlink_to(tmp_path / "afuera", target_is_directory=True)
    with pytest.raises(SandboxError, match="fuera"):
        resolve_workdir(ws, "atajo")


@posix_only
def test_nested_symlink_escape_is_rejected(ws, tmp_path):
    (ws / "proj" / "sub" / "salto").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(SandboxError, match="fuera"):
        resolve_workdir(ws, "proj/sub/salto/afuera")


@posix_only
def test_symlink_pointing_inside_is_fine(ws):
    (ws / "alias").symlink_to(ws / "proj" / "sub", target_is_directory=True)
    assert resolve_workdir(ws, "alias") == (ws / "proj" / "sub").resolve()


@posix_only
def test_symlink_loop_is_rejected(ws):
    (ws / "a").symlink_to(ws / "b")
    (ws / "b").symlink_to(ws / "a")
    with pytest.raises(SandboxError):
        resolve_workdir(ws, "a")


@posix_only
def test_workspace_root_symlink_is_resolved(ws, tmp_path):
    link = tmp_path / "ws-link"
    link.symlink_to(ws, target_is_directory=True)
    assert resolve_workdir(link, "proj") == (ws / "proj").resolve()


# --- ejecutable -----------------------------------------------------------------


def test_executable_from_path(ws, tmp_path, monkeypatch):
    exe = make_exe(tmp_path / "bin")
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    assert resolve_executable("claude", ws) == exe.resolve()


def test_executable_absolute(ws, tmp_path):
    exe = make_exe(tmp_path / "bin")
    assert resolve_executable(str(exe), ws) == exe.resolve()


def test_executable_not_found(ws, tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "vacío"))
    with pytest.raises(SandboxError, match="PATH"):
        resolve_executable("claude", ws)
    with pytest.raises(SandboxError, match="no existe"):
        resolve_executable(str(tmp_path / "bin" / "nada"), ws)


def test_executable_relative_path_rejected(ws):
    with pytest.raises(SandboxError, match="absoluta"):
        resolve_executable("bin/claude", ws)
    with pytest.raises(SandboxError, match="absoluta"):
        resolve_executable(r"..\claude.exe", ws)


def test_executable_planted_in_workspace_via_path(ws, monkeypatch):
    make_exe(ws / "proj")
    monkeypatch.setenv("PATH", str(ws / "proj"))
    with pytest.raises(SandboxError, match="dentro de la carpeta de trabajo"):
        resolve_executable("claude", ws)


def test_executable_absolute_inside_workspace(ws):
    exe = make_exe(ws / "proj")
    with pytest.raises(SandboxError, match="dentro de la carpeta de trabajo"):
        resolve_executable(str(exe), ws)


@posix_only
def test_executable_symlink_into_workspace(ws, tmp_path, monkeypatch):
    real = make_exe(ws / "proj")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "claude").symlink_to(real)
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    with pytest.raises(SandboxError, match="dentro de la carpeta de trabajo"):
        resolve_executable("claude", ws)


@posix_only
def test_non_executable_file_rejected(ws, tmp_path):
    exe = make_exe(tmp_path / "bin")
    exe.chmod(0o644)
    with pytest.raises(SandboxError):
        resolve_executable(str(exe), ws)
