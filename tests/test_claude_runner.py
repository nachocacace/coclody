from __future__ import annotations

import dataclasses
import json
import os
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from coclody.config import ClaudeConfig
from coclody.orchestrator import claude_runner
from coclody.orchestrator.claude_runner import build_invocation, run_claude
from coclody.orchestrator.sandbox import SandboxError

SESSION = "123e4567-e89b-12d3-a456-426614174000"

FAKE_CLAUDE = textwrap.dedent(
    """
    import json, os, sys, time
    mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
    prompt = sys.stdin.read()
    report = {"argv": sys.argv[1:], "cwd": os.getcwd(), "env": sorted(os.environ), "stdin": prompt}
    if mode == "ok":
        print(json.dumps({"type": "result", "is_error": False, "session_id": "%s", "result": json.dumps(report)}))
    elif mode == "is_error":
        print(json.dumps({"type": "result", "is_error": True, "session_id": "%s", "result": "falló algo"}))
        print("detalle en stderr", file=sys.stderr)
        sys.exit(1)
    elif mode == "bad_session":
        print(json.dumps({"type": "result", "is_error": False, "session_id": "--resume x", "result": "ok"}))
    elif mode == "garbage":
        print("esto no es json")
    elif mode == "huge":
        sys.stdout.write("x" * 3_000_000)
    elif mode == "sleep":
        time.sleep(60)
    """
    % (SESSION, SESSION)
)


@pytest.fixture
def ws(tmp_path):
    root = tmp_path / "ws"
    (root / "proj").mkdir(parents=True)
    (tmp_path / "afuera").mkdir()
    return root


@pytest.fixture
def fake_claude(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if sys.platform == "win32":
        exe = bindir / "claude.cmd"
        exe.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
    else:
        exe = bindir / "claude"
        exe.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
        exe.chmod(0o755)
    return exe


def cfg(ws, exe, tools=("Read", "Edit", "Bash(git status:*)"), timeout=1):
    return ClaudeConfig(workspace_root=ws, executable=str(exe), allowed_tools=tuple(tools), timeout_minutes=timeout)


def report_of(result):
    return json.loads(result.result_text)


# --- armado de la invocación ----------------------------------------------------


def test_argv_flags(ws, fake_claude):
    inv = build_invocation(cfg(ws, fake_claude), "hacé algo", workdir_rel="proj")
    assert inv.argv == (
        str(fake_claude.resolve()),
        "-p",
        "--output-format", "json",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--strict-mcp-config",
        "--setting-sources", "",
        "--tools", "Read,Edit,Bash",
        "--allowedTools", "Read,Edit,Bash(git status:*)",
    )
    assert inv.cwd == (ws / "proj").resolve()
    assert inv.stdin == "hacé algo".encode()
    assert inv.timeout_s == 60
    assert (inv.max_stdout_bytes, inv.max_stderr_bytes) == (2_000_000, 200_000)
    assert "--dangerously-skip-permissions" not in inv.argv
    assert "bypassPermissions" not in inv.argv


def test_no_tools_disables_all(ws, fake_claude):
    inv = build_invocation(cfg(ws, fake_claude, tools=()), "x")
    i = inv.argv.index("--tools")
    assert inv.argv[i + 1] == ""
    assert "--allowedTools" not in inv.argv


def test_duplicate_tools_are_collapsed(ws, fake_claude):
    inv = build_invocation(cfg(ws, fake_claude, tools=("Read", "Read", "Bash(ls:*)", "Bash(pwd)")), "x")
    assert inv.argv[inv.argv.index("--tools") + 1] == "Read,Bash"


@pytest.mark.parametrize(
    "prompt",
    [
        "--dangerously-skip-permissions",
        "-p --permission-mode bypassPermissions",
        "; rm -rf ~",
        "$(whoami)",
        "`id`",
        "& del /s /q C:\\",
        "%USERPROFILE%",
        "línea 1\n--resume " + SESSION,
        '" --tools default "',
    ],
)
def test_prompt_never_reaches_argv(ws, fake_claude, prompt):
    base = build_invocation(cfg(ws, fake_claude), "texto normal")
    inv = build_invocation(cfg(ws, fake_claude), prompt)
    assert inv.argv == base.argv
    assert inv.stdin == prompt.encode()


@pytest.mark.parametrize(
    "tools",
    [
        ("Bash",),
        ("--dangerously-skip-permissions",),
        ("Read,Bash",),
        ("Bash(git status:*),Bash",),
        ("Bash(x) --tools default",),
        ("Read\n--permission-mode",),
        (" Read",),
        ("",),
    ],
)
def test_tools_revalidated_even_if_config_was_bypassed(ws, fake_claude, tools):
    with pytest.raises(SandboxError, match="herramientas inválidas"):
        build_invocation(cfg(ws, fake_claude, tools=tools), "x")


@pytest.mark.parametrize("session", ["--dangerously-skip-permissions", "abc", SESSION + "\n", SESSION + " -p", "", 123])
def test_resume_session_must_be_uuid(ws, fake_claude, session):
    with pytest.raises(SandboxError, match="session_id"):
        build_invocation(cfg(ws, fake_claude), "x", resume_session=session)


def test_resume_session_valid(ws, fake_claude):
    inv = build_invocation(cfg(ws, fake_claude), "x", resume_session=SESSION)
    assert inv.argv[-2:] == ("--resume", SESSION)


@pytest.mark.parametrize("workdir", ["../afuera", "/", r"C:\Windows", "proj/../../afuera", "no-existe"])
def test_workdir_escape_rejected(ws, fake_claude, workdir):
    with pytest.raises(SandboxError):
        build_invocation(cfg(ws, fake_claude), "x", workdir_rel=workdir)


def test_executable_inside_workspace_rejected(ws, fake_claude):
    planted = ws / "proj" / fake_claude.name
    planted.write_bytes(fake_claude.read_bytes())
    planted.chmod(0o755)
    with pytest.raises(SandboxError, match="dentro de la carpeta de trabajo"):
        build_invocation(cfg(ws, planted), "x")


@pytest.mark.parametrize(
    "prompt",
    ["", "   ", None, "a\x00b", pytest.param("x" * (claude_runner.MAX_PROMPT_CHARS + 1), id="demasiado-largo")],
)
def test_prompt_validation(ws, fake_claude, prompt):
    with pytest.raises(SandboxError):
        build_invocation(cfg(ws, fake_claude), prompt)


def test_env_drops_paid_api_and_coclody_vars(ws, fake_claude):
    base = {
        "PATH": "/usr/bin",
        "HOME": "/home/x",
        "ANTHROPIC_API_KEY": "sk-ant-xxx",
        "anthropic_base_url": "http://evil",
        "ANTHROPIC_AUTH_TOKEN": "t",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "COCLODY_DATA_DIR": "/tmp/x",
        "CLAUDE_CONFIG_DIR": "/home/x/.claude",
    }
    env = build_invocation(cfg(ws, fake_claude), "x", base_env=base).env
    assert env == {"PATH": "/usr/bin", "HOME": "/home/x", "CLAUDE_CONFIG_DIR": "/home/x/.claude"}


@pytest.mark.parametrize("tool", ["Bash(echo %PATH%)", 'Bash(echo "x")', "Bash(a & b)", "Bash(a | b)", "Bash(a ^ b)", "Bash(a > b)", "Bash(a!)"])
def test_batch_wrapper_rejects_cmd_metacharacters(ws, tmp_path, tool):
    exe = tmp_path / "npm" / "claude.cmd"
    exe.parent.mkdir()
    exe.write_text("@echo off\r\n")
    exe.chmod(0o755)
    with pytest.raises(SandboxError, match="cmd.exe"):
        build_invocation(cfg(ws, exe, tools=("Read", tool)), "x")


def test_batch_wrapper_accepts_safe_arguments(ws, tmp_path):
    exe = tmp_path / "npm" / "CLAUDE.CMD"
    exe.parent.mkdir()
    exe.write_text("@echo off\r\n")
    exe.chmod(0o755)
    inv = build_invocation(cfg(ws, exe), "prompt con & | % y \" que va por stdin", resume_session=SESSION)
    assert inv.argv[0] == str(exe.resolve())


# --- ejecución con un claude falso --------------------------------------------


def test_run_ok(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-no-debe-llegar")
    prompt = "--dangerously-skip-permissions; $(whoami) ñ"
    result = run_claude(build_invocation(cfg(ws, fake_claude), prompt, workdir_rel="proj"))
    assert result.status == "ok" and result.exit_code == 0
    assert result.session_id == SESSION
    report = report_of(result)
    assert report["stdin"] == prompt
    assert os.path.samefile(report["cwd"], ws / "proj")
    assert "ANTHROPIC_API_KEY" not in report["env"]
    assert "--dangerously-skip-permissions" not in report["argv"]
    assert report["argv"][-2:] == ["--allowedTools", "Read,Edit,Bash(git status:*)"]


def test_run_is_error(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "is_error")
    result = run_claude(build_invocation(cfg(ws, fake_claude), "x"))
    assert result.status == "error" and result.exit_code == 1
    assert "falló algo" in result.summary and "detalle en stderr" in result.summary


def test_run_garbage_output(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "garbage")
    result = run_claude(build_invocation(cfg(ws, fake_claude), "x"))
    assert result.status == "error" and "JSON" in result.summary
    assert result.session_id is None


def test_run_invalid_session_in_output_is_ignored(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "bad_session")
    result = run_claude(build_invocation(cfg(ws, fake_claude), "x"))
    assert result.status == "ok" and result.session_id is None


def test_run_huge_output(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "huge")
    result = run_claude(build_invocation(cfg(ws, fake_claude), "x"))
    assert result.status == "error" and result.stdout_truncated
    assert "2000000 bytes" in result.summary


def test_output_limit_comes_from_config(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    small = dataclasses.replace(cfg(ws, fake_claude), max_stdout_bytes=100)
    result = run_claude(build_invocation(small, "x"))
    assert result.status == "error" and result.stdout_truncated and "100 bytes" in result.summary


def test_run_timeout(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    inv = dataclasses.replace(build_invocation(cfg(ws, fake_claude), "x"), timeout_s=1)
    result = run_claude(inv)
    assert result.status == "timeout" and "timeout" in result.summary


def test_run_cancel(ws, fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    result = run_claude(build_invocation(cfg(ws, fake_claude), "x"), cancel=cancel)
    assert result.status == "killed"


def test_summary_is_bounded(ws, fake_claude, monkeypatch):
    monkeypatch.setattr(claude_runner, "SUMMARY_MAX_CHARS", 50)
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")
    result = run_claude(build_invocation(cfg(ws, fake_claude), "y" * 500))
    assert len(result.summary) == 50 and result.summary.endswith("…")
