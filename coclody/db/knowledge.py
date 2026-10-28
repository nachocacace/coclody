"""Reglas y contexto por proyecto: la base de conocimiento que recibe el cerebro (modo v2).

- ``rules``: reglas fijas de cómo resolver, que escribe SOLO el usuario (Telegram o CLI). El cerebro
  nunca las edita: ninguna función que escriba reglas se llama desde su código, y un test estático
  lo verifica (ver tests/test_knowledge.py).
- ``context``: contexto del proyecto. El usuario puede agregar entradas ya aprobadas; el cerebro solo
  puede *proponer* borradores (``draft``), que quedan pendientes hasta que el usuario los apruebe o
  rechace por Telegram. Una entrada aprobada se puede archivar.

Presupuesto de tamaño (el cerebro recibe todo esto en su prompt): las reglas habilitadas y el contexto
aprobado tienen un tope de caracteres que se impone al escribir/aprobar, no al armar el prompt. Si no
entra, se rechaza en el momento, con un mensaje claro, en vez de recortar en silencio más adelante.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from coclody.db.connection import transaction
from coclody.db.repo import NotFoundError, ValidationError, _int, _text

RULES_BUDGET = 6_000
CONTEXT_BUDGET = 10_000
RULE_MAX = 2_000
CONTEXT_MAX = 4_000


@dataclass(frozen=True)
class Rule:
    id: int
    project_id: int
    text: str
    enabled: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Context:
    id: int
    project_id: int
    text: str
    status: str
    proposed_by: str
    brain_decision_id: int | None
    decided_by: int | None
    decided_at: str | None
    created_at: str


# --- reglas (solo el usuario) -------------------------------------------------------------------


def add_rule(conn: sqlite3.Connection, project_id: int, text: str) -> Rule:
    pid = _int(project_id, "project_id", 1, 2**53)
    body = _text(text, "regla", RULE_MAX)
    with transaction(conn):
        used = _sum_len(conn, "SELECT text FROM rules WHERE project_id = ? AND enabled = 1", pid)
        if used + len(body) > RULES_BUDGET:
            raise ValidationError(
                f"las reglas habilitadas superarían el tope de {RULES_BUDGET} caracteres "
                f"(usados {used}, nueva {len(body)}); desactivá o acortá alguna"
            )
        cur = conn.execute("INSERT INTO rules (project_id, text) VALUES (?, ?)", (pid, body))
        return _get(conn, Rule, "rules", cur.lastrowid)


def list_rules(conn: sqlite3.Connection, project_id: int, enabled_only: bool = False) -> list[Rule]:
    sql = "SELECT * FROM rules WHERE project_id = ?" + (" AND enabled = 1" if enabled_only else "") + " ORDER BY id"
    return [Rule(**dict(r)) for r in conn.execute(sql, (project_id,))]


def set_rule_enabled(conn: sqlite3.Connection, project_id: int, rule_id: int, enabled: bool) -> Rule:
    with transaction(conn):
        rule = _owned(conn, Rule, "rules", project_id, rule_id, "regla")
        if enabled and not rule.enabled:
            used = _sum_len(conn, "SELECT text FROM rules WHERE project_id = ? AND enabled = 1", rule.project_id)
            if used + len(rule.text) > RULES_BUDGET:
                raise ValidationError(f"reactivarla superaría el tope de {RULES_BUDGET} caracteres de reglas")
        conn.execute("UPDATE rules SET enabled = ?, updated_at = datetime('now') WHERE id = ?", (int(enabled), rule.id))
        return _get(conn, Rule, "rules", rule.id)


def delete_rule(conn: sqlite3.Connection, project_id: int, rule_id: int) -> bool:
    with transaction(conn):
        _owned(conn, Rule, "rules", project_id, rule_id, "regla")
        return conn.execute("DELETE FROM rules WHERE id = ?", (rule_id,)).rowcount == 1


# --- contexto -----------------------------------------------------------------------------------


def add_user_context(conn: sqlite3.Connection, project_id: int, text: str, decided_by: int) -> Context:
    """El usuario agrega contexto ya aprobado (no pasa por borrador)."""
    pid = _int(project_id, "project_id", 1, 2**53)
    body = _text(text, "contexto", CONTEXT_MAX)
    by = _int(decided_by, "decided_by", 0, 2**53)  # 0 = sistema/CLI (no hay chat)
    with transaction(conn):
        _check_context_budget(conn, pid, len(body))
        cur = conn.execute(
            "INSERT INTO context (project_id, text, status, proposed_by, decided_by, decided_at)"
            " VALUES (?, ?, 'approved', 'user', ?, datetime('now'))",
            (pid, body, by),
        )
        return _get(conn, Context, "context", cur.lastrowid)


def propose_context(conn: sqlite3.Connection, project_id: int, text: str,
                    brain_decision_id: int | None = None) -> Context:
    """El cerebro propone un borrador de contexto: queda pendiente de aprobación del usuario."""
    pid = _int(project_id, "project_id", 1, 2**53)
    body = _text(text, "contexto", CONTEXT_MAX)
    with transaction(conn):
        cur = conn.execute(
            "INSERT INTO context (project_id, text, status, proposed_by, brain_decision_id)"
            " VALUES (?, ?, 'draft', 'brain', ?)",
            (pid, body, brain_decision_id),
        )
        return _get(conn, Context, "context", cur.lastrowid)


def list_context(conn: sqlite3.Connection, project_id: int, statuses: tuple[str, ...] | None = None) -> list[Context]:
    if statuses is None:
        rows = conn.execute("SELECT * FROM context WHERE project_id = ? ORDER BY id", (project_id,))
    else:
        marks = ",".join("?" * len(statuses))
        rows = conn.execute(
            f"SELECT * FROM context WHERE project_id = ? AND status IN ({marks}) ORDER BY id", (project_id, *statuses)
        )
    return [Context(**dict(r)) for r in rows]


def decide_context(conn: sqlite3.Connection, project_id: int, context_id: int, approve: bool, decided_by: int) -> Context:
    """Aprueba o rechaza un borrador. Aprobar respeta el tope de contexto aprobado."""
    by = _int(decided_by, "decided_by", 0, 2**53)  # 0 = sistema/CLI (no hay chat)
    with transaction(conn):
        entry = _owned(conn, Context, "context", project_id, context_id, "contexto")
        if entry.status != "draft":
            raise ValidationError(f"el contexto {entry.id} no es un borrador (está '{entry.status}')")
        if approve:
            _check_context_budget(conn, entry.project_id, len(entry.text))
        conn.execute(
            "UPDATE context SET status = ?, decided_by = ?, decided_at = datetime('now') WHERE id = ?",
            ("approved" if approve else "rejected", by, entry.id),
        )
        return _get(conn, Context, "context", entry.id)


def archive_context(conn: sqlite3.Connection, project_id: int, context_id: int) -> Context:
    """Saca de circulación una entrada aprobada (deja de ir al cerebro), sin borrarla."""
    with transaction(conn):
        entry = _owned(conn, Context, "context", project_id, context_id, "contexto")
        if entry.status != "approved":
            raise ValidationError(f"solo se archiva contexto aprobado; el {entry.id} está '{entry.status}'")
        conn.execute("UPDATE context SET status = 'archived' WHERE id = ?", (entry.id,))
        return _get(conn, Context, "context", entry.id)


# --- lo que recibe el cerebro ------------------------------------------------------------------


def enabled_rules_text(conn: sqlite3.Connection, project_id: int) -> str:
    return "\n".join(f"- {r.text}" for r in list_rules(conn, project_id, enabled_only=True))


def approved_context_text(conn: sqlite3.Connection, project_id: int) -> str:
    return "\n".join(f"- {c.text}" for c in list_context(conn, project_id, statuses=("approved",)))


# --- helpers ------------------------------------------------------------------------------------


def _check_context_budget(conn: sqlite3.Connection, project_id: int, extra: int) -> None:
    used = _sum_len(conn, "SELECT text FROM context WHERE project_id = ? AND status = 'approved'", project_id)
    if used + extra > CONTEXT_BUDGET:
        raise ValidationError(
            f"el contexto aprobado superaría el tope de {CONTEXT_BUDGET} caracteres "
            f"(usados {used}, nuevo {extra}); archivá o acortá alguna entrada"
        )


def _sum_len(conn: sqlite3.Connection, sql: str, project_id: int) -> int:
    return sum(len(row[0]) for row in conn.execute(sql, (project_id,)))


def _owned(conn, cls, table: str, project_id: int, row_id: int, label: str):
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    if row is None or row["project_id"] != project_id:
        raise NotFoundError(f"no existe {label} #{row_id} en este proyecto")
    return cls(**dict(row))


def _get(conn, cls, table: str, row_id: int):
    return cls(**dict(conn.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()))
