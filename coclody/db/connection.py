"""Conexión a SQLite.

La conexión trabaja en modo autocommit (``isolation_level=None``) y las
transacciones son explícitas con ``transaction()``. Así nunca queda una
transacción implícita abierta y cada operación de escritura es atómica.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

MIN_SQLITE = (3, 37, 0)  # tablas STRICT


def connect(path: Path) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise RuntimeError(
            f"SQLite {sqlite3.sqlite_version} es muy viejo; hace falta {'.'.join(map(str, MIN_SQLITE))} o más."
        )
    _create_private(path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


DbFactory = Callable[[], AbstractContextManager[sqlite3.Connection]]


def connection_factory(path: Path) -> DbFactory:
    """Fábrica de conexiones de corta vida.

    Cada uso abre y cierra su propia conexión: así el bot (event loop) y el
    worker (hilo aparte) nunca comparten una conexión entre hilos.
    """

    @contextmanager
    def factory() -> Iterator[sqlite3.Connection]:
        conn = connect(path)
        try:
            yield conn
        finally:
            conn.close()

    return factory


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """``BEGIN IMMEDIATE`` ... ``COMMIT``, o ``ROLLBACK`` si algo falla."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _create_private(path: Path) -> None:
    """Crea el archivo de la base con permisos 0600 en POSIX antes de que SQLite lo cree.

    SQLite copia esos permisos a los archivos -wal y -shm.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return
    os.close(fd)
