from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest

from coclody.config import DEFAULT_ALLOWED_TOOLS, TEMPLATE, ConfigError, load_config, write_template


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def write(app_paths, text: str) -> Path:
    app_paths.config_file.write_text(text, encoding="utf-8")
    return app_paths.config_file


def toml_path(path: Path) -> str:
    return str(path).replace("\\", "\\\\")


def minimal(workspace: Path) -> str:
    return f"""
[telegram]
allowed_chat_ids = [111, 222]

[claude]
workspace_root = "{toml_path(workspace)}"
"""


def problems(app_paths) -> list[str]:
    with pytest.raises(ConfigError) as exc:
        load_config(app_paths)
    return exc.value.problems


def test_minimal_config_gets_defaults(app_paths, workspace):
    write(app_paths, minimal(workspace))
    cfg = load_config(app_paths)
    assert cfg.telegram.allowed_chat_ids == frozenset({111, 222})
    assert cfg.claude.workspace_root == workspace.resolve()
    assert cfg.claude.executable == "claude"
    assert cfg.claude.allowed_tools == DEFAULT_ALLOWED_TOOLS
    assert all(t.endswith("(./**)") for t in DEFAULT_ALLOWED_TOOLS)
    assert not any(t.startswith("Bash") for t in DEFAULT_ALLOWED_TOOLS)
    assert cfg.claude.timeout_minutes == 60
    assert (cfg.claude.max_stdout_bytes, cfg.claude.max_stderr_bytes) == (2_000_000, 200_000)
    assert cfg.updates.enabled is False
    assert cfg.updates.github_repo is None
    assert cfg.updates.check_interval_hours == 24


def test_full_config(app_paths, workspace):
    write(
        app_paths,
        minimal(workspace)
        + """
executable = "claude"
allowed_tools = ["Read", "Bash(git status:*)"]
timeout_minutes = 15

[updates]
enabled = true
github_repo = "nacho/coclody"
check_interval_hours = 6
""",
    )
    cfg = load_config(app_paths)
    assert cfg.claude.allowed_tools == ("Read", "Bash(git status:*)")
    assert cfg.claude.timeout_minutes == 15
    assert cfg.updates.enabled is True
    assert cfg.updates.github_repo == "nacho/coclody"


def test_missing_file(app_paths):
    assert "no existe" in problems(app_paths)[0]


def test_invalid_toml(app_paths):
    write(app_paths, "[telegram\n")
    assert "TOML" in problems(app_paths)[0]


def test_missing_required_keys(app_paths):
    write(app_paths, "")
    found = problems(app_paths)
    assert "telegram.allowed_chat_ids: falta (es obligatorio)" in found
    assert "claude.workspace_root: falta (es obligatorio)" in found


def test_reports_all_problems_together(app_paths, workspace):
    write(
        app_paths,
        minimal(workspace).replace("[111, 222]", "[true, -5]")
        + 'timeout_minutes = "60"\ncolor = "azul"\n[otra]\nx = 1\n',
    )
    found = problems(app_paths)
    keys = {p.split(":", 1)[0] for p in found}
    assert keys == {"telegram.allowed_chat_ids", "claude.timeout_minutes", "claude.color", "otra"}


@pytest.mark.parametrize("ids", ["[]", "123", "[0]", "[-100123]", "[1.5]", "[true]", '["123"]'])
def test_bad_chat_ids(app_paths, workspace, ids):
    write(app_paths, minimal(workspace).replace("[111, 222]", ids))
    assert problems(app_paths)[0].startswith("telegram.allowed_chat_ids:")


@pytest.mark.parametrize("value", [0, 1441, True, "10", 2.5])
def test_timeout_range_and_type(app_paths, workspace, value):
    rendered = {True: "true", "10": '"10"'}.get(value, str(value))
    write(app_paths, minimal(workspace) + f"timeout_minutes = {rendered}\n")
    assert problems(app_paths)[0].startswith("claude.timeout_minutes:")


def test_output_limits(app_paths, workspace):
    write(app_paths, minimal(workspace) + "max_stdout_bytes = 100000\nmax_stderr_bytes = 5000\n")
    cfg = load_config(app_paths)
    assert (cfg.claude.max_stdout_bytes, cfg.claude.max_stderr_bytes) == (100_000, 5000)


@pytest.mark.parametrize(
    ("key", "value"),
    [("max_stdout_bytes", "1000"), ("max_stdout_bytes", "60000000"), ("max_stderr_bytes", "100"), ("max_stderr_bytes", '"5000"')],
)
def test_output_limits_range(app_paths, workspace, key, value):
    write(app_paths, minimal(workspace) + f"{key} = {value}\n")
    assert problems(app_paths)[0].startswith(f"claude.{key}:")


def test_workspace_must_exist(app_paths, tmp_path):
    write(app_paths, minimal(tmp_path / "nope"))
    assert "no existe" in problems(app_paths)[0]


def test_workspace_must_be_absolute(app_paths):
    write(app_paths, minimal(Path("relative/dir")))
    assert "absoluta" in problems(app_paths)[0]


def test_workspace_cannot_be_home(app_paths):
    write(app_paths, minimal(Path.home()))
    assert problems(app_paths)[0].startswith("claude.workspace_root:")


def test_workspace_cannot_contain_app_data(app_paths, tmp_path):
    # app_paths vive en tmp_path/appdata: tmp_path la contiene.
    write(app_paths, minimal(tmp_path))
    assert "carpeta de CoClody" in problems(app_paths)[0]


def test_workspace_cannot_be_inside_app_data(app_paths):
    inside = app_paths.data_dir / "work"
    inside.mkdir()
    write(app_paths, minimal(inside))
    assert "carpeta de CoClody" in problems(app_paths)[0]


@pytest.mark.skipif(os.name != "posix", reason="symlinks sin privilegios solo en POSIX")
def test_workspace_symlink_is_resolved(app_paths, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(app_paths.data_dir, target_is_directory=True)
    write(app_paths, minimal(link))
    assert "carpeta de CoClody" in problems(app_paths)[0]


@pytest.mark.parametrize("tools", ['["Read\\n"]', '["Bash(a,b)"]', '["Bash"]', '["Read", 3]', '["rm -rf"]', '"Read"', '["Bash(a)(b)"]'])
def test_bad_tools(app_paths, workspace, tools):
    write(app_paths, minimal(workspace) + f"allowed_tools = {tools}\n")
    assert problems(app_paths)[0].startswith("claude.allowed_tools:")


@pytest.mark.parametrize("exe", ['""', '"bin/claude"', r'"..\\claude.exe"', "5"])
def test_bad_executable(app_paths, workspace, exe):
    write(app_paths, minimal(workspace) + f"executable = {exe}\n")
    assert problems(app_paths)[0].startswith("claude.executable:")


def test_updates_enabled_requires_repo(app_paths, workspace):
    write(app_paths, minimal(workspace) + "[updates]\nenabled = true\n")
    assert problems(app_paths) == ["updates.github_repo: es obligatorio si updates.enabled = true"]


@pytest.mark.parametrize("repo", ['"a/b\\n"', '"coclody"', '"a/b/c"', '"https://github.com/a/b"', '"a b/c"'])
def test_bad_repo(app_paths, workspace, repo):
    write(app_paths, minimal(workspace) + f"[updates]\ngithub_repo = {repo}\n")
    assert problems(app_paths)[0].startswith("updates.github_repo:")


def test_section_must_be_table(app_paths, workspace):
    write(app_paths, 'updates = "no"\n' + minimal(workspace))
    assert "updates: tiene que ser una sección [updates]" in problems(app_paths)


def test_error_message_lists_path_and_problems(app_paths):
    write(app_paths, "")
    with pytest.raises(ConfigError) as exc:
        load_config(app_paths)
    message = str(exc.value)
    assert str(app_paths.config_file) in message
    assert "telegram.allowed_chat_ids" in message


def test_template_is_valid_toml_and_fails_until_filled(app_paths, workspace):
    write_template(app_paths.config_file)
    assert tomllib.loads(TEMPLATE)
    if os.name == "posix":
        assert stat.S_IMODE(app_paths.config_file.stat().st_mode) == 0o600
    found = problems(app_paths)
    assert {p.split(":", 1)[0] for p in found} == {"telegram.allowed_chat_ids", "claude.workspace_root"}

    filled = TEMPLATE.replace("allowed_chat_ids = []", "allowed_chat_ids = [42]").replace(
        'workspace_root = ""', f'workspace_root = "{toml_path(workspace)}"'
    )
    write(app_paths, filled)
    assert load_config(app_paths).telegram.allowed_chat_ids == frozenset({42})


def test_template_never_overwrites(app_paths):
    write(app_paths, "# mío\n")
    with pytest.raises(FileExistsError):
        write_template(app_paths.config_file)
    assert app_paths.config_file.read_text(encoding="utf-8") == "# mío\n"


def test_template_tools_are_the_restrictive_default():
    assert tuple(tomllib.loads(TEMPLATE)["claude"]["allowed_tools"]) == DEFAULT_ALLOWED_TOOLS
    assert "git status" not in TEMPLATE


def test_tool_warnings():
    from coclody.config import tool_warnings

    assert tool_warnings(DEFAULT_ALLOWED_TOOLS) == []
    found = tool_warnings(["Read", "Write(~/**)", "Grep(./**)", "Bash(npm test)", "WebFetch"])
    assert [w.split(":", 1)[0] for w in found] == ["Read", "Write(~/**)", "Bash(npm test)", "WebFetch"]
    assert "escalarlo" in found[2]


def test_bare_bash_error_warns_about_escalation(app_paths, workspace):
    write(app_paths, minimal(workspace) + 'allowed_tools = ["Bash"]\n')
    (problem,) = problems(app_paths)
    assert "escalar" in problem and "git status" not in problem
