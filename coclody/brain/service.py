"""Una invocación del cerebro, en modo simulacro: decide y registra, pero NO ejecuta nada.

En esta etapa no existe ningún camino de código que lleve a cabo una decisión: todas se guardan con
estado ``dry_run`` en la auditoría. La ejecución real llega en la etapa 5, detrás del esquema cerrado
y la validación todo-o-nada. Así el simulacro es inocuo por construcción, no por una bandera.

La auditoría (``brain_runs`` + ``brain_decisions``) deja reconstruir cada decisión: el prompt exacto
(con las reglas, el contexto y los resúmenes que vio, todo inline), su SHA-256, los ids de lo que
entró, la salida cruda del modelo y cada decisión con su JSON.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from coclody.config import Config
from coclody.db import DbFactory, knowledge, repo
from coclody.db import projects as prepo
from coclody.brain.prompt import BrainInputs, build_prompt
from coclody.brain.runner import run_brain
from coclody.brain.schema import Decision, SchemaError, parse_response
from coclody.orchestrator.gitops import Git  # noqa: F401  (tipo usado por quien llama)


@dataclass(frozen=True)
class BrainOutcome:
    run_id: int
    parse_status: str          # ok | invalid | error
    decisions: list[Decision]  # vacío salvo parse_status == ok
    error: str | None
    plan: str                  # texto para mostrar por Telegram


def gather_inputs(conn, config: Config, project: prepo.Project, trigger: str) -> tuple[BrainInputs, frozenset[int]]:
    """Arma las entradas del cerebro desde la base. Devuelve (inputs, ids de preguntas abiertas)."""
    workers = [w.name for w in prepo.list_workers(conn, project.id)]
    rules = [r for r in knowledge.list_rules(conn, project.id, enabled_only=True)]
    contexts = knowledge.list_context(conn, project.id, statuses=("approved",))

    tasks = repo.list_tasks(conn, statuses=("pending", "queued", "running", "waiting_input", "done", "failed"),
                            limit=1000)
    tasks = [t for t in tasks if t.project_id == project.id]
    active = [t for t in tasks if t.status in ("pending", "queued", "running", "waiting_input")]
    queue = [f"#{t.id} [{t.worker or '—'}] {t.status}: {t.title}" for t in active]

    # Preguntas abiertas de los workers del proyecto (las puede responder el cerebro o derivarlas).
    open_q, q_ids = [], []
    for t in tasks:
        for q in repo.list_open_questions(conn, task_id=t.id):
            open_q.append(f"#{q.id} (tarea #{t.id}, {t.worker or '—'}): {q.question}")
            q_ids.append(q.id)

    # Último resumen de cada tarea con ejecuciones, del más nuevo al más viejo.
    summaries, summary_run_ids = [], []
    for t in sorted(tasks, key=lambda x: x.id, reverse=True):
        runs = repo.list_runs(conn, t.id)
        if runs and runs[-1].summary:
            summaries.append((f"tarea #{t.id} ({t.worker or '—'}, {t.status})", runs[-1].summary))
            summary_run_ids.append(runs[-1].id)

    inputs = BrainInputs(
        workers=workers,
        rules_text="\n".join(f"- {r.text}" for r in rules),
        context_text="\n".join(f"- {c.text}" for c in contexts),
        events=[trigger],
        questions=open_q,
        summaries=summaries,
        queue=queue,
        input_ids={
            "rules": [r.id for r in rules],
            "context": [c.id for c in contexts],
            "questions": q_ids,
            "summary_runs": summary_run_ids,
            "queue_tasks": [t.id for t in active],
        },
    )
    return inputs, frozenset(q_ids)


def run_once(db: DbFactory, config: Config, git, brain_dir: Path, hooks_dir: Path, empty_config: Path,
             trigger: str) -> BrainOutcome | None:
    """Corre el cerebro una vez en simulacro. None si no hay proyecto activo."""
    with db() as conn:
        project = prepo.active_project(conn)
        if project is None:
            return None
        inputs, open_ids = gather_inputs(conn, config, project, trigger)
        workers = frozenset(inputs.workers)

    built = build_prompt(inputs, config.orchestrator.prompt_budget_chars)
    with db() as conn:
        run = repo.start_brain_run(conn, project.id, trigger, built.text, json.dumps(inputs.input_ids), dry_run=True)

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
                            f"El cerebro devolvió algo que no cumple el esquema; lo descarté entero.\nMotivo: {exc}")

    with db() as conn:
        repo.finish_brain_run(conn, run.id, result.result_text, "ok", None, duration)
        for i, d in enumerate(decisions):
            # SIMULACRO: se registra, no se ejecuta. result_ref queda vacío (no se creó nada).
            repo.add_brain_decision(conn, run.id, i, d.action, d.payload(), status="dry_run")
    return BrainOutcome(run.id, "ok", decisions, None, _plan(run.id, decisions))


def _finish(db: DbFactory, run_id: int, raw: str | None, status: str, error: str, duration: float) -> None:
    with db() as conn:
        repo.finish_brain_run(conn, run_id, raw, status, error, duration)


def _plan(run_id: int, decisions: list[Decision]) -> str:
    lines = [f"🧠 Simulacro del cerebro (run #{run_id}). Esto es lo que haría, no lo ejecuté:"]
    for i, d in enumerate(decisions, 1):
        lines.append(f"{i}. {_describe(d)}\n   porque: {d.reason}")
    lines.append("\n(No se tocó nada. La ejecución real llega en una etapa futura.)")
    return "\n".join(lines)


def _describe(d: Decision) -> str:
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
