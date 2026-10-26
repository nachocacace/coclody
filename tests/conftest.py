from __future__ import annotations

import pytest

from coclody.db import connect, migrate
from coclody.paths import AppPaths


@pytest.fixture
def app_paths(tmp_path):
    paths = AppPaths.under(tmp_path / "appdata")
    paths.ensure()
    return paths


@pytest.fixture
def conn(app_paths):
    connection = connect(app_paths.db_file)
    migrate(connection)
    yield connection
    connection.close()
