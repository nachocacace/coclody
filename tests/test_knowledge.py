"""Etapa 3: reglas y contexto por proyecto, por Telegram y CLI, con aprobación de borradores.

- Las reglas las edita solo el usuario; el cerebro nunca (verificado por un guard estático).
- El contexto del usuario entra aprobado; el del cerebro entra como borrador y necesita aprobación.
- Topes de tamaño impuestos al escribir/aprobar.
- Todo cuelga del proyecto activo.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from coclody.db import knowledge
from coclody.db import projects as prepo
from coclody.db.repo import NotFoundError, ValidationError

ME = 777


@pytest.fixture
def proj(conn):
    conn.execute("INSERT INTO projects (name, root, base_branch, active) VALUES ('web', '/tmp/web', 'main', 1)")
    return prepo.active_project(conn).id


# --- reglas (solo el usuario) ----------------------------------------------------------------------------


def test_add_list_toggle_delete_rule(conn, proj):
    r = knowledge.add_rule(conn, proj, "Usá pytest para todo")
    assert r.enabled == 1 and r.text == "Usá pytest para todo"
    assert [x.id for x in knowledge.list_rules(conn, proj)] == [r.id]
    r2 = knowledge.set_rule_enabled(conn, proj, r.id, False)
    assert r2.enabled == 0
    assert knowledge.list_rules(conn, proj, enabled_only=True) == []
    assert knowledge.delete_rule(conn, proj, r.id) is True
    assert knowledge.list_rules(conn, proj) == []


def test_rules_budget_enforced_on_write(conn, proj):
    knowledge.add_rule(conn, proj, "x" * 2000)
    knowledge.add_rule(conn, proj, "y" * 2000)
    knowledge.add_rule(conn, proj, "z" * 1900)
    with pytest.raises(ValidationError, match="tope"):
        knowledge.add_rule(conn, proj, "w" * 200)  # pasaría de 6000
    # Al desactivar una, libera lugar.
    rules = knowledge.list_rules(conn, proj)
    knowledge.set_rule_enabled(conn, proj, rules[0].id, False)
    assert knowledge.add_rule(conn, proj, "w" * 200).enabled == 1


def test_reactivating_a_rule_respects_the_budget(conn, proj):
    a = knowledge.add_rule(conn, proj, "a" * 2000)
    knowledge.set_rule_enabled(conn, proj, a.id, False)
    knowledge.add_rule(conn, proj, "b" * 2000)
    knowledge.add_rule(conn, proj, "c" * 2000)
    knowledge.add_rule(conn, proj, "d" * 1500)  # habilitadas: 5500
    with pytest.raises(ValidationError, match="tope"):
        knowledge.set_rule_enabled(conn, proj, a.id, True)  # 5500 + 2000 > 6000


def test_rules_are_scoped_to_their_project(conn, proj):
    conn.execute("INSERT INTO projects (name, root, base_branch) VALUES ('otro', '/tmp/otro', 'main')")
    other = prepo.get_project(conn, "otro").id
    r = knowledge.add_rule(conn, other, "regla ajena")
    assert knowledge.list_rules(conn, proj) == []
    with pytest.raises(NotFoundError):
        knowledge.delete_rule(conn, proj, r.id)
    with pytest.raises(NotFoundError):
        knowledge.set_rule_enabled(conn, proj, r.id, False)


# --- contexto ------------------------------------------------------------------------------------------


def test_user_context_is_approved_directly(conn, proj):
    c = knowledge.add_user_context(conn, proj, "El deploy es manual", ME)
    assert (c.status, c.proposed_by, c.decided_by) == ("approved", "user", ME)
    assert "El deploy es manual" in knowledge.approved_context_text(conn, proj)


def test_brain_context_is_a_draft_until_decided(conn, proj):
    d = knowledge.propose_context(conn, proj, "Parece que usan Postgres")
    assert (d.status, d.proposed_by) == ("draft", "brain")
    assert knowledge.approved_context_text(conn, proj) == ""  # un borrador no va al cerebro
    approved = knowledge.decide_context(conn, proj, d.id, approve=True, decided_by=ME)
    assert approved.status == "approved" and approved.decided_by == ME
    assert "Postgres" in knowledge.approved_context_text(conn, proj)


def test_rejected_draft_never_becomes_context(conn, proj):
    d = knowledge.propose_context(conn, proj, "dato dudoso")
    r = knowledge.decide_context(conn, proj, d.id, approve=False, decided_by=ME)
    assert r.status == "rejected"
    assert knowledge.approved_context_text(conn, proj) == ""
    with pytest.raises(ValidationError, match="no es un borrador"):
        knowledge.decide_context(conn, proj, d.id, approve=True, decided_by=ME)


def test_archive_removes_from_what_the_brain_sees(conn, proj):
    c = knowledge.add_user_context(conn, proj, "dato viejo", ME)
    knowledge.archive_context(conn, proj, c.id)
    assert knowledge.approved_context_text(conn, proj) == ""
    with pytest.raises(ValidationError, match="aprobado"):
        knowledge.archive_context(conn, proj, c.id)  # ya no está aprobado


def test_context_budget_enforced_on_user_add_and_on_approve(conn, proj):
    knowledge.add_user_context(conn, proj, "a" * 4000, ME)
    knowledge.add_user_context(conn, proj, "b" * 4000, ME)
    with pytest.raises(ValidationError, match="tope"):
        knowledge.add_user_context(conn, proj, "c" * 2001, ME)  # pasaría de 10000
    d = knowledge.propose_context(conn, proj, "c" * 2001)
    with pytest.raises(ValidationError, match="tope"):
        knowledge.decide_context(conn, proj, d.id, approve=True, decided_by=ME)
    assert knowledge.decide_context(conn, proj, d.id, approve=False, decided_by=ME).status == "rejected"


# --- garantía: el cerebro nunca edita reglas (guard estático) --------------------------------------------

RULE_WRITERS = {"add_rule", "set_rule_enabled", "delete_rule"}
# Módulos que SÍ pueden escribir reglas: las edita el usuario por CLI o Telegram.
ALLOWED = {"coclody/cli.py", "coclody/telegram_bot/commands.py", "coclody/db/knowledge.py"}
ROOT = Path(__file__).resolve().parent.parent


def test_no_brain_or_worker_code_writes_rules():
    offenders = []
    for path in (ROOT / "coclody").rglob("*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in ALLOWED:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
                if name in RULE_WRITERS:
                    offenders.append(f"{rel}:{node.lineno} {name}")
    assert offenders == [], offenders


def test_rule_writers_still_exist():
    # Si se renombra un escritor de reglas, el guard de arriba quedaría ciego: esto lo detecta.
    assert RULE_WRITERS <= set(dir(knowledge))
