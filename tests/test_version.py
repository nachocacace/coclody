from __future__ import annotations

import tomllib
from pathlib import Path

from coclody import __version__
from coclody.updater.manifest import parse_version

ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_and_package_versions_match():
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["project"]["version"] == __version__


def test_version_is_valid_for_manifests():
    parse_version(__version__)
