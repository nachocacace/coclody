"""Esquema de la base y migraciones.

El SQL vive como constantes de Python (no en archivos .sql) para que viaje
dentro del ejecutable de PyInstaller sin declarar archivos de datos.

La versión del esquema se guarda en ``PRAGMA user_version``. Cada migración
corre en su propia transacción junto con el cambio de versión: o se aplica
entera o no se aplica.
"""

from __future__ import annotations

import sqlite3

from coclody.db.connection import transaction

_V1 = """
CREATE TABLE tasks (
    id          INTEGER PRIMARY KEY,
    title       TEXT    NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    description TEXT             CHECK (length(description) <= 10000),
    status      TEXT    NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'waiting_input', 'queued',
                                  'running', 'done', 'failed', 'cancelled')),
    priority    INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -100 AND 100),
    workdir_rel TEXT             CHECK (length(workdir_rel) <= 500),
    created_by  INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at TEXT
) STRICT;

CREATE TABLE questions (
    id            INTEGER PRIMARY KEY,
    task_id       INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    question      TEXT    NOT NULL CHECK (length(question) BETWEEN 1 AND 4000),
    answer        TEXT             CHECK (length(answer) <= 4000),
    status        TEXT    NOT NULL DEFAULT 'open'
                  CHECK (status IN ('open', 'answered', 'expired')),
    tg_message_id INTEGER,
    asked_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    answered_at   TEXT,
    answered_by   INTEGER
) STRICT;

CREATE TABLE runs (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    session_id TEXT             CHECK (length(session_id) <= 200),
    started_at TEXT    NOT NULL DEFAULT (datetime('now')),
    ended_at   TEXT,
    exit_code  INTEGER,
    status     TEXT    NOT NULL DEFAULT 'running'
               CHECK (status IN ('running', 'ok', 'error', 'timeout', 'killed')),
    summary    TEXT             CHECK (length(summary) <= 10000)
) STRICT;

CREATE TABLE task_events (
    id         INTEGER PRIMARY KEY,
    task_id    INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id     INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('status_change', 'progress', 'question', 'error')),
    message    TEXT    NOT NULL CHECK (length(message) <= 4000),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    notified   INTEGER NOT NULL DEFAULT 0 CHECK (notified IN (0, 1))
) STRICT;

CREATE INDEX idx_tasks_status     ON tasks(status, priority DESC);
CREATE INDEX idx_questions_task   ON questions(task_id);
CREATE INDEX idx_questions_open   ON questions(status) WHERE status = 'open';
CREATE UNIQUE INDEX idx_questions_tg_message ON questions(tg_message_id) WHERE tg_message_id IS NOT NULL;
CREATE INDEX idx_runs_task        ON runs(task_id);
CREATE INDEX idx_events_task      ON task_events(task_id);
CREATE INDEX idx_events_pending   ON task_events(notified) WHERE notified = 0;
"""

# Orden estricto: la migración N lleva la base de user_version N-1 a N.
MIGRATIONS: tuple[str, ...] = (_V1,)
SCHEMA_VERSION = len(MIGRATIONS)


class SchemaTooNewError(RuntimeError):
    pass


def current_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> int:
    """Aplica las migraciones pendientes y devuelve la versión final."""
    version = current_version(conn)
    if version > SCHEMA_VERSION:
        # Base creada por una versión más nueva de CoClody: no se toca.
        raise SchemaTooNewError(
            f"La base tiene esquema v{version} y esta versión de CoClody solo conoce hasta v{SCHEMA_VERSION}."
        )
    for target in range(version + 1, SCHEMA_VERSION + 1):
        with transaction(conn):
            for statement in _split(MIGRATIONS[target - 1]):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {int(target)}")
    return SCHEMA_VERSION


def _split(script: str) -> list[str]:
    """Separa un script en sentencias completas (executescript haría COMMIT por su cuenta)."""
    statements, buffer = [], ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            if buffer.strip():
                statements.append(buffer.strip())
            buffer = ""
    if buffer.strip():
        raise ValueError(f"Sentencia SQL incompleta en la migración: {buffer.strip()[:60]}")
    return statements
