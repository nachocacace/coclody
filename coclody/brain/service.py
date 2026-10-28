"""Una invocación del cerebro y la ejecución de sus decisiones, en tres modos.

- **Simulacro** (``brain_dry_run = true``, default): decide y registra, no ejecuta nada. Inocuo por
  construcción: no se llama al ejecutor.
- **Aprobación** (``brain_dry_run = false`` y ``brain_approval = true``, el freno de mano por defecto
  al pasar a real): cada decisión queda pendiente y te llega por Telegram para aprobar; se ejecuta
  recién cuando la aprobás.
- **Autónomo** (``brain_approval = false``): cada decisión se ejecuta en el momento.

En los tres casos la auditoría (``brain_runs`` + ``brain_decisions``) deja reconstruir qué vio y qué
decidió. La validación contra el esquema cerrado es todo-o-nada: si la respuesta no cierra, no se toma
ninguna decisión.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from coclody.config import Config
from coclody.db import DbFactory, knowledge, repo
from coclody.db import projects as prepo
from coclody.brain import executor
from coclody.brain.prompt import BrainInputs, build_prompt
from coclody.brain.runner import run_brain
from coclody.brain.schema import Decision, SchemaError, decision_from_payload, parse_response


@dataclass(frozen=True)
class BrainOutcome:
    run_id: int
    parse_status: str                         # ok | invalid | error
    decisions: list[Decision]
    error: str | None
    plan: str
    mode: str = "dry_run"                     # dry_run | approval | autonomous
    pending: list[tuple[int, Decision]] = field(default_factory=list)   # (decision_id, decisión) a aprobar
    brain_question_ids: list[int] = field(default_factory=list)         # preguntas creadas que hay que enviar


def gather_inputs(conn, config: Config, project: prepo.Project, trigger: str) -> tuple[BrainInputs, frozenset[int]]:
    workers = [w.name for w in prepo.list_workers(conn, project.id)]
    rules = list(knowledge.list_rules(conn, project.id, enabled_only=True))
    contexts = knowledge.list_context(conn, project.id, statuses=("approved",))

    tasks = repo.list_tasks(conn, statuses=("pending", "queued", "running", "waiting_input", "done", "failed"),
                            limit=1000)
    tasks = [t for t in tasks if t.project_id == project.id]
    active = [t for t in tasks if t.status in ("pending", "queued", "running", "waiting_input")]
    queue = [f"#{t.id} [{t.worker or '—'}] {t.status}: {t.title}" for t in active]

    open_q, q_ids = [], []
    for t in tasks:
        for q in repo.list_open_questions(conn, task_id=t.id):
            open_q.append(f"#{q.id} (tarea #{t.id}, {t.worker or '—'}): {q.question}")
            q_ids.append(q.id)

    summaries, summary_run_ids = [], []
    for t in sorted(tasks, key=lambda x: x.id, reverse=True):
        runs = repo.list_runs(conn, t.id)
        if runs and runs[-1].summary:
            summaries.append((f"tarea #{t.id} ({t.worker or '—'}, {t.status})", runs[-1].summary))
            summary_run_ids.append(runs[-1].id)

    pending_asks = [q.question for q in repo.list_open_brain_questions(conn, project.id)]

    inputs = BrainInputs(
        workers=workers,
        rules_text="\n".join(f"- {r.text}" for r in rules),
        context_text="\n".join(f"- {c.text}" for c in contexts),
        events=[trigger],
        questions=open_q,
        pending_asks=pending_asks,
        summaries=summaries,
        queue=queue,
        input_ids={"rules": [r.id for r in rules], "context": [c.id for c in contexts], "questions": q_ids,
                   "summary_runs": summary_run_ids, "queue_tasks": [t.id for t in active]},
    )
    return inputs, frozenset(q_ids)


def run_once(db: DbFactory, config: Config, git, brain_dir: Path, hooks_dir: Path, empty_config: Path,
             trigger: str, event_ids: list[int] | None = None) -> BrainOutcome | None:
    """Corre el cerebro una vez. None si no hay proyecto activo."""
    dry = config.orchestrator.brain_dry_run
    with db() as conn:
        project = prepo.active_project(conn)
        if project is None:
            return None
        inputs, open_ids = gather_inputs(conn, config, project, trigger)
        workers = frozenset(inputs.workers)

    built = build_prompt(inputs, config.orchestrator.prompt_budget_chars)
    with db() as conn:
        run = repo.start_brain_run(conn, project.id, trigger, built.text, json.dumps(inputs.input_ids), dry_run=dry)
        if event_ids:
            repo.consume_brain_events(conn, event_ids, run.id)

    started = time.monotonic()
    result = run_brain(config.claude, built.text, brain_dir, hooks_dir, empty_config)
    duration = time.monotonic() - started

    if result.status != "ok":
        _finish(db, run.id, result.result_text, "error", f"Claude Code: {result.summary}", duration)
        return BrainOutcome(run.id, "error", [], result.summary, f"El cerebro no pudo correr: {result.summary}")
    try:
        decisions = parse_response(result.result_text, workers=workers, open_question_ids=open_ids)
    except SchemaError as exc:
        _finish(db, run.id, result.result_text, "invalid", str(exc), duration)
        return BrainOutcome(run.id, "invalid", [], str(exc),
                            f"El cerebro devolvió algo fuera del esquema; lo descarté entero.\nMotivo: {exc}")

    with db() as conn:
        repo.finish_brain_run(conn, run.id, result.result_text, "ok", None, duration)

    if dry:
        _record_all(db, run.id, decisions, status="dry_run")
        return BrainOutcome(run.id, "ok", decisions, None, _plan(run.id, decisions, "dry_run"), mode="dry_run")

    if config.orchestrator.brain_approval:
        pending = []
        with db() as conn:
            for i, d in enumerate(decisions):
                row = repo.add_brain_decision(conn, run.id, i, d.action, d.payload(), status="dry_run")
                repo.add_brain_approval(conn, row.id)
                pending.append((row.id, d))
        return BrainOutcome(run.id, "ok", decisions, None, _plan(run.id, decisions, "approval"),
                            mode="approval", pending=pending)

    # autónomo: ejecuta cada decisión en el momento.
    applied, bq_ids = [], []
    with db() as conn:
        for i, d in enumerate(decisions):
            row = repo.add_brain_decision(conn, run.id, i, d.action, d.payload(), status="dry_run")
            done = executor.execute(conn, config, project, d, row.id)
            repo.set_brain_decision_result(conn, row.id, "executed" if done.ok else "rejected", done.result_ref)
            applied.append((d, done))
            if done.brain_question_id:
                bq_ids.append(done.brain_question_id)
    return BrainOutcome(run.id, "ok", decisions, None, _plan_applied(run.id, applied), mode="autonomous",
                        brain_question_ids=bq_ids)


def approve_decision(db: DbFactory, config: Config, decision_id: int, approved: bool,
                     decided_by: int) -> tuple[str, int | None]:
    """Resuelve una decisión pendiente desde un botón. Devuelve (texto, brain_question_id a enviar o None)."""
    with db() as conn:
        decision = repo.get_brain_decision(conn, decision_id)
        if decision is None:
            return "Esa decisión no existe.", None
        project = prepo.active_project(conn)
        try:
            repo.resolve_approval(conn, decision_id, approved, decided_by)
        except repo.RepoError as exc:
            return f"No pude: {exc}", None
        if not approved:
            repo.set_brain_decision_result(conn, decision_id, "rejected", "rechazada por vos")
            return "Rechazada. No la ejecuté.", None
        payload = json.loads(decision.payload)
        workers = frozenset(w.name for w in prepo.list_workers(conn, project.id)) if project else frozenset()
        open_ids = frozenset(q.id for t in repo.list_tasks(conn, statuses=("waiting_input",), limit=1000)
                             if t.project_id == (project.id if project else -1)
                             for q in repo.list_open_questions(conn, task_id=t.id))

    try:
        d = decision_from_payload(payload, workers=workers, open_question_ids=open_ids)
    except SchemaError as exc:
        with db() as conn:
            repo.set_brain_decision_result(conn, decision_id, "rejected", f"inválida al aprobar: {exc}")
        return f"No la ejecuté: ya no es válida ({exc}).", None
    with db() as conn:
        done = executor.execute(conn, config, project, d, decision_id)
        repo.set_brain_decision_result(conn, decision_id, "executed" if done.ok else "rejected", done.result_ref)
    return (f"Aprobada: {done.detail}." if done.ok else f"Aprobada, pero no se pudo: {done.detail}.",
            done.brain_question_id)


def _record_all(db: DbFactory, run_id: int, decisions: list[Decision], status: str) -> None:
    with db() as conn:
        for i, d in enumerate(decisions):
            repo.add_brain_decision(conn, run_id, i, d.action, d.payload(), status=status)


def _finish(db: DbFactory, run_id: int, raw: str | None, status: str, error: str, duration: float) -> None:
    with db() as conn:
        repo.finish_brain_run(conn, run_id, raw, status, error, duration)


def _plan(run_id: int, decisions: list[Decision], mode: str) -> str:
    cola = {"dry_run": "Esto es lo que haría, no lo ejecuté:",
            "approval": "Esto propone; te mando cada una para aprobar:"}[mode]
    lines = [f"🧠 Cerebro (run #{run_id}). {cola}"]
    for i, d in enumerate(decisions, 1):
        lines.append(f"{i}. {describe(d)}\n   porque: {d.reason}")
    if mode == "dry_run":
        lines.append("\n(No se tocó nada. La ejecución real llega al apagar brain_dry_run.)")
    return "\n".join(lines)


def _plan_applied(run_id: int, applied: list[tuple[Decision, executor.Executed]]) -> str:
    lines = [f"🧠 Cerebro (run #{run_id}, autónomo):"]
    for d, done in applied:
        mark = "✓" if done.ok else "⚠️"
        lines.append(f"{mark} {done.detail}\n   porque: {d.reason}")
    return "\n".join(lines)


def decision_detail(db: DbFactory, decision_id: int) -> str:
    """El porqué (y el contexto) de una decisión, para el botón ℹ Contexto."""
    with db() as conn:
        row = repo.get_brain_decision(conn, decision_id)
    if row is None:
        return "No encuentro esa decisión."
    payload = json.loads(row.payload)
    lines = [describe_payload(payload)]
    if payload.get("reason"):
        lines.append(f"Porque: {payload['reason']}")
    if payload.get("based_on"):
        lines.append(f"Se basa en: {payload['based_on']}")
    if payload.get("description"):
        lines.append(f"Detalle de la tarea: {payload['description']}")
    return "\n".join(lines)


def describe_payload(payload: dict) -> str:
    action = payload.get("action")
    if action == "enqueue":
        return f"Encolar para {payload.get('worker')}: «{payload.get('title')}»"
    if action == "ask":
        return f"Preguntarte: {payload.get('question')}"
    if action == "answer_worker_question":
        return f"Responder la pregunta #{payload.get('question_id')}: {payload.get('answer')}"
    if action == "propose_context":
        return f"Proponer contexto: {payload.get('text')}"
    return "Esperar"


def describe(d: Decision) -> str:
    f = d.fields
    if d.action == "enqueue":
        return f"Encolar para {f['worker']}: «{f['title']}»"
    if d.action == "ask":
        return f"Preguntarte ({f['kind']}): {f['question']}"
    if d.action == "answer_worker_question":
        return f"Responder la pregunta #{f['question_id']}: {f['answer']}"
    if d.action == "propose_context":
        return f"Proponer contexto: {f['text']}"
    return "Esperar (no hay nada que hacer)"
