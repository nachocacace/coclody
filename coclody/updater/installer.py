"""Reemplazo del ejecutable y limpieza posterior.

En Windows no se puede sobreescribir un .exe que está corriendo, pero sí
renombrarlo. Secuencia:

1. Copiar el archivo verificado junto al ejecutable como ``coclody.exe.new``
   (mismo volumen, así los renombres son atómicos) y volver a verificar el hash.
2. Renombrar el ejecutable actual a ``coclody.exe.old``.
3. Renombrar ``.new`` a ``coclody.exe``. Si falla, se deshace el paso 2.

El proceso actual sigue corriendo desde ``.old``; la versión nueva se usa al
reiniciar, y ese arranque borra el ``.old``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

OLD_SUFFIX = ".old"
NEW_SUFFIX = ".new"
PART_SUFFIX = ".part"


class InstallError(Exception):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def replace_executable(exe: Path, staged: Path, sha256: str, size: int) -> None:
    """Instala ``staged`` en lugar de ``exe``. Lanza InstallError dejando ``exe`` intacto."""
    new = exe.with_name(exe.name + NEW_SUFFIX)
    old = exe.with_name(exe.name + OLD_SUFFIX)
    if old.exists():
        try:
            old.unlink()
        except OSError:
            # Lo usa el proceso de una actualización anterior todavía no reiniciada.
            raise InstallError("hay una actualización anterior pendiente de reinicio; reiniciá CoClody primero") from None
    try:
        shutil.copyfile(staged, new)
        if new.stat().st_size != size or sha256_file(new) != sha256:
            raise InstallError("la copia junto al ejecutable no coincide con el manifiesto")
        if os.name == "posix":
            new.chmod(0o755)
        os.replace(exe, old)
    except InstallError:
        new.unlink(missing_ok=True)
        raise
    except OSError as exc:
        new.unlink(missing_ok=True)
        raise InstallError(f"no se pudo preparar el reemplazo: {exc}") from None
    try:
        os.replace(new, exe)
    except OSError as exc:
        try:
            os.replace(old, exe)
        except OSError:
            log.critical("No se pudo restaurar %s desde %s", exe, old)
        new.unlink(missing_ok=True)
        raise InstallError(f"no se pudo instalar la versión nueva: {exc}") from None


def cleanup_previous(exe: Path | None, updates_dir: Path) -> list[Path]:
    """Al arrancar: borra el ``.old`` de una actualización ya aplicada y descargas a medias."""
    removed = []
    candidates = []
    if exe is not None:
        candidates += [exe.with_name(exe.name + OLD_SUFFIX), exe.with_name(exe.name + NEW_SUFFIX)]
    if updates_dir.is_dir():
        candidates += [p for p in updates_dir.iterdir() if p.is_file()]
    for path in candidates:
        if not path.exists():
            continue
        try:
            path.unlink()
        except OSError as exc:
            log.warning("No se pudo borrar %s: %s", path, exc)
        else:
            removed.append(path)
    return removed
