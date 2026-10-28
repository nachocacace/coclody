"""Clasifica archivos tocados por un commit que conviene revisar a mano antes de mergear.

El caso que más importa (lo pidió el usuario): un workflow de ``.github/workflows`` editado ejecuta
código en CI con sus secretos cuando él pushea. También: configs que ejecutan código al correr tests
o builds (``conftest.py``, ``package.json``, ``Makefile``…), que vuelven "ilusoria" la lista de
comandos; y configuración de editores o de Claude que corre sola al abrir el proyecto.

Esto es una ayuda para la revisión, no una barrera: no bloquea nada, solo lo señala.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

# categoría -> patrones (sobre la ruta relativa con "/", en minúsculas)
_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("CI", (r"\.github/workflows/", r"\.gitlab-ci\.yml$", r"(^|/)\.circleci/", r"(^|/)azure-pipelines\.yml$",
            r"(^|/)\.travis\.yml$", r"(^|/)jenkinsfile$", r"(^|/)\.github/actions/")),
    ("build/test", (r"(^|/)conftest\.py$", r"(^|/)package\.json$", r"(^|/)makefile$", r"(^|/)justfile$",
                    r"(^|/)rakefile$", r"(^|/)noxfile\.py$", r"(^|/)tox\.ini$", r"(^|/)setup\.py$",
                    r"(^|/)pyproject\.toml$", r"(^|/)pre-commit-config\.yaml$", r"(^|/)\.pre-commit-config\.yaml$",
                    r"(^|/)gradlew?$", r"(^|/)build\.gradle", r"(^|/)pom\.xml$", r"(^|/)cargo\.toml$",
                    r"(^|/)dockerfile$", r"(^|/)docker-compose\.ya?ml$")),
    ("hooks/scripts", (r"(^|/)\.husky/", r"(^|/)scripts/", r"(^|/)\.envrc$")),
    ("editor/Claude", (r"(^|/)\.vscode/", r"(^|/)\.idea/", r"(^|/)\.claude/", r"(^|/)claude\.md$",
                       r"(^|/)\.cursorrules$", r"(^|/)\.devcontainer/")),
    ("dependencias", (r"(^|/)requirements[^/]*\.txt$", r"(^|/)package-lock\.json$", r"(^|/)yarn\.lock$",
                      r"(^|/)pnpm-lock\.yaml$", r"(^|/)poetry\.lock$", r"(^|/)cargo\.lock$")),
)
_COMPILED = tuple((cat, re.compile("|".join(pats))) for cat, pats in _RULES)


def classify(paths: list[str]) -> list[str]:
    """Categorías sensibles presentes en una lista de rutas, en orden fijo y sin repetir."""
    norm = [PurePosixPath(p.replace("\\", "/")).as_posix().lower().lstrip("/") for p in paths]
    found = []
    for category, pattern in _COMPILED:
        if category not in found and any(pattern.search(p) for p in norm):
            found.append(category)
    return found
