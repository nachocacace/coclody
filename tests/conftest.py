from __future__ import annotations

import pytest

from coclody.db import connect, migrate
from coclody.paths import AppPaths

# pytest guarda el ID del test en la variable de entorno PYTEST_CURRENT_TEST, y
# Windows limita las variables de entorno a 32767 caracteres: un parámetro
# enorme sin id explícito (p. ej. un prompt de 100 000 caracteres) rompe el test
# solo en Windows. Se corta al recolectar, en cualquier plataforma.
MAX_NODEID_CHARS = 500


def pytest_collection_modifyitems(config, items):
    too_long = [item.nodeid[:120] + "…" for item in items if len(item.nodeid) > MAX_NODEID_CHARS]
    if too_long:
        raise pytest.UsageError(
            f"IDs de test de más de {MAX_NODEID_CHARS} caracteres (usá pytest.param(..., id=...)):\n"
            + "\n".join(too_long)
        )


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
