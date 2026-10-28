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

# v2:
# - Solo algunos eventos se notifican por Telegram (los cambios a done, failed
#   y waiting_input). El resto queda como historial en la base.
# - Los message_id de Telegram son únicos por chat, no globales: la pregunta se
#   identifica por (tg_chat_id, tg_message_id).
_V2 = """
ALTER TABLE task_events ADD COLUMN notify INTEGER NOT NULL DEFAULT 0 CHECK (notify IN (0, 1));
DROP INDEX idx_events_pending;
CREATE INDEX idx_events_pending ON task_events(id) WHERE notify = 1 AND notified = 0;

ALTER TABLE questions ADD COLUMN tg_chat_id INTEGER;
DROP INDEX idx_questions_tg_message;
CREATE UNIQUE INDEX idx_questions_tg_message ON questions(tg_chat_id, tg_message_id)
    WHERE tg_message_id IS NOT NULL;
"""

# v3: registro de envíos por destinatario. Un evento se marca como notificado
# recién cuando todos sus destinatarios tienen su envío registrado, así un
# reintento solo cubre a los que fallaron. ``part`` es 0 para el mensaje del
# evento, o el id de la pregunta enviada (un evento waiting_input puede llevar
# varias). El message_id sirve para responder con reply desde cualquier chat.
_V3 = """
CREATE TABLE notification_deliveries (
    event_id      INTEGER NOT NULL REFERENCES task_events(id) ON DELETE CASCADE,
    chat_id       INTEGER NOT NULL,
    part          INTEGER NOT NULL DEFAULT 0 CHECK (part >= 0),
    status        TEXT    NOT NULL CHECK (status IN ('sent', 'rejected')),
    tg_message_id INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (event_id, chat_id, part)
) STRICT;

CREATE INDEX idx_deliveries_message ON notification_deliveries(chat_id, tg_message_id)
    WHERE tg_message_id IS NOT NULL;
"""

# v4: tipo de pregunta. ``yes_no`` cuando Claude la marcó como de sí/no: se
# envía con botones Sí/No además de poder responderse con texto.
_V4 = """
ALTER TABLE questions ADD COLUMN kind TEXT NOT NULL DEFAULT 'open' CHECK (kind IN ('open', 'yes_no'));
"""

# v5: pedidos de título del botón "Nueva tarea" que esperan respuesta. Se
# guardan para que una respuesta siga valiendo después de reiniciar CoClody.
_V5 = """
CREATE TABLE task_prompts (
    chat_id    INTEGER NOT NULL CHECK (chat_id > 0),
    message_id INTEGER NOT NULL CHECK (message_id > 0),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (chat_id, message_id)
) STRICT;
"""

# v6: orquestador (modo v2). Todo cuelga de un proyecto: carpeta raíz con sus
# worktrees, reglas y contexto; uno activo por vez. Sin el modo v2 activado
# estas tablas quedan vacías y las tareas tienen project_id/worker en NULL.
# Las tablas del cerebro (eventos, invocaciones, decisiones, preguntas) son la
# auditoría completa: una tarea creada por el cerebro apunta a su decisión, la
# decisión a su invocación, y la invocación guarda el prompt y sus entradas.
_V6 = """
CREATE TABLE projects (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE CHECK (length(name) BETWEEN 1 AND 32),
    root        TEXT    NOT NULL UNIQUE CHECK (length(root) BETWEEN 1 AND 4096),
    base_branch TEXT    NOT NULL CHECK (length(base_branch) BETWEEN 1 AND 200),
    active      INTEGER NOT NULL DEFAULT 0 CHECK (active IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE UNIQUE INDEX idx_projects_one_active ON projects(active) WHERE active = 1;

CREATE TABLE workers (
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL CHECK (length(name) BETWEEN 1 AND 16),
    branch          TEXT    NOT NULL CHECK (length(branch) BETWEEN 1 AND 200),
    path_rel        TEXT    NOT NULL CHECK (length(path_rel) BETWEEN 1 AND 1024),
    state           TEXT    NOT NULL DEFAULT 'idle'
                    CHECK (state IN ('idle', 'running', 'paused', 'error')),
    current_task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    last_head       TEXT    CHECK (last_head IS NULL OR length(last_head) BETWEEN 40 AND 64),
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (project_id, name)
) STRICT;

CREATE TABLE brain_runs (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    trigger_events TEXT    NOT NULL,
    prompt         TEXT    NOT NULL,
    prompt_sha256  TEXT    NOT NULL CHECK (length(prompt_sha256) = 64),
    inputs         TEXT    NOT NULL,
    raw_output     TEXT    CHECK (raw_output IS NULL OR length(raw_output) <= 65536),
    parse_status   TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (parse_status IN ('pending', 'ok', 'invalid', 'error')),
    error          TEXT,
    dry_run        INTEGER NOT NULL CHECK (dry_run IN (0, 1)),
    started_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at    TEXT,
    duration_s     REAL
) STRICT;

CREATE TABLE brain_decisions (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES brain_runs(id) ON DELETE CASCADE,
    idx        INTEGER NOT NULL CHECK (idx >= 0),
    action     TEXT    NOT NULL
               CHECK (action IN ('enqueue', 'ask', 'answer_worker_question', 'propose_context', 'wait')),
    payload    TEXT    NOT NULL,
    status     TEXT    NOT NULL CHECK (status IN ('executed', 'rejected', 'dry_run')),
    result_ref TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (run_id, idx)
) STRICT;

ALTER TABLE tasks ADD COLUMN project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL;
ALTER TABLE tasks ADD COLUMN worker TEXT CHECK (worker IS NULL OR length(worker) BETWEEN 1 AND 16);
ALTER TABLE tasks ADD COLUMN origin TEXT NOT NULL DEFAULT 'user' CHECK (origin IN ('user', 'brain'));
ALTER TABLE tasks ADD COLUMN brain_decision_id INTEGER REFERENCES brain_decisions(id) ON DELETE SET NULL;

CREATE INDEX idx_tasks_dispatch ON tasks(project_id, worker, status, priority, id);

CREATE TABLE task_commits (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    worker      TEXT    NOT NULL,
    base_sha    TEXT    NOT NULL CHECK (length(base_sha) BETWEEN 40 AND 64),
    head_sha    TEXT    NOT NULL CHECK (length(head_sha) BETWEEN 40 AND 64),
    files       INTEGER NOT NULL CHECK (files >= 0),
    insertions  INTEGER NOT NULL CHECK (insertions >= 0),
    deletions   INTEGER NOT NULL CHECK (deletions >= 0),
    big_change  INTEGER NOT NULL DEFAULT 0 CHECK (big_change IN (0, 1)),
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE TABLE rules (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text       TEXT    NOT NULL CHECK (length(text) BETWEEN 1 AND 2000),
    enabled    INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE TABLE context (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    text              TEXT    NOT NULL CHECK (length(text) BETWEEN 1 AND 4000),
    status            TEXT    NOT NULL DEFAULT 'draft'
                      CHECK (status IN ('draft', 'approved', 'rejected', 'archived')),
    proposed_by       TEXT    NOT NULL CHECK (proposed_by IN ('user', 'brain')),
    brain_decision_id INTEGER REFERENCES brain_decisions(id) ON DELETE SET NULL,
    decided_by        INTEGER,
    decided_at        TEXT,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE TABLE brain_events (
    id              INTEGER PRIMARY KEY,
    project_id      INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL
                    CHECK (kind IN ('worker_idle', 'worker_question', 'task_failed', 'user_answer', 'user_request')),
    ref_id          INTEGER,
    consumed_by_run INTEGER REFERENCES brain_runs(id) ON DELETE SET NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
) STRICT;

CREATE INDEX idx_brain_events_pending ON brain_events(project_id, id) WHERE consumed_by_run IS NULL;

CREATE TABLE brain_questions (
    id                INTEGER PRIMARY KEY,
    project_id        INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    question          TEXT    NOT NULL CHECK (length(question) BETWEEN 1 AND 4000),
    kind              TEXT    NOT NULL DEFAULT 'open' CHECK (kind IN ('open', 'yes_no')),
    status            TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'answered', 'expired')),
    answer            TEXT    CHECK (answer IS NULL OR length(answer) <= 4000),
    brain_decision_id INTEGER REFERENCES brain_decisions(id) ON DELETE SET NULL,
    asked_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    answered_at       TEXT,
    answered_by       INTEGER
) STRICT;

CREATE TABLE brain_question_deliveries (
    question_id   INTEGER NOT NULL REFERENCES brain_questions(id) ON DELETE CASCADE,
    chat_id       INTEGER NOT NULL,
    status        TEXT    NOT NULL CHECK (status IN ('sent', 'rejected')),
    tg_message_id INTEGER,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (question_id, chat_id)
) STRICT;

CREATE TABLE runtime_state (
    key        TEXT NOT NULL PRIMARY KEY CHECK (length(key) BETWEEN 1 AND 64),
    value      TEXT NOT NULL CHECK (length(value) <= 4000),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
) STRICT;
"""

# Orden estricto: la migración N lleva la base de user_version N-1 a N.
# v7: categorías de archivos sensibles tocados por un commit (CI, build, configs que ejecutan código),
# para marcarlos en el aviso de la tarea y, más adelante, en /resumen. Vacío = nada sensible.
_V7 = """
ALTER TABLE task_commits ADD COLUMN sensitive TEXT NOT NULL DEFAULT '';
"""

MIGRATIONS: tuple[str, ...] = (_V1, _V2, _V3, _V4, _V5, _V6, _V7)
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
