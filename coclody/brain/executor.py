"""Ejecuta una decisión del cerebro ya validada contra el esquema cerrado.

Solo se llega acá cuando NO es simulacro y la decisión fue aprobada (freno de mano) o la aprobación
está apagada (autónomo). Cada acción hace lo mínimo y nada más de lo que el esquema puede expresar:
no hay forma de correr comandos, tocar git ni cambiar la config. Los topes (tareas autónomas por día,
cola por worker) se chequean acá: si una decisión los supera, no se ejecuta y se informa el motivo.

Las preguntas del cerebro (``ask``) se registran acá; el envío por Telegram lo hace quien tiene el bot.
"""

from __future__ import annotations

from dataclasses import dataclass

from coclody.config import Config
from coclody.db import knowledge, repo
from coclody.db import projects as prepo
from coclody.db.repo import RepoError
from coclody.brain.schema import Decision


@dataclass(frozen=True)
class Executed:
    ok: bool
    result_ref: str | None   # p. ej. "task:5", "brain_question:2", "context:3"
    detail: str              # para el aviso y la auditoría
    brain_question_id: int | None = None  # si creó una pregunta que hay que enviar por Telegram


def execute(conn, config: Config, project: prepo.Project, decision: Decision,
            brain_decision_id: int) -> Executed:
    orch = config.orchestrator
    f = decision.fields
    try:
        if decision.action == "wait":
            return Executed(True, None, "no hizo nada (esperar)")

        if decision.action == "enqueue":
            if repo.count_brain_tasks_today(conn, project.id) >= orch.autonomous_tasks_per_day:
                return Executed(False, None, f"tope de {orch.autonomous_tasks_per_day} tareas autónomas por día")
            if repo.count_queued_for(conn, project.id, f["worker"]) >= orch.max_queue_per_worker:
                return Executed(False, None, f"{f['worker']} ya tiene {orch.max_queue_per_worker} tareas en cola")
            task = repo.create_task(conn, f["title"], description=f["description"], project_id=project.id)
            task = repo.assign_task(conn, task.id, project.id, f["worker"])
            repo.mark_task_from_brain(conn, task.id, brain_decision_id)
            return Executed(True, f"task:{task.id}", f"encoló la tarea #{task.id} para {f['worker']}")

        if decision.action == "answer_worker_question":
            qid = f["question_id"]
            question = repo.get_question(conn, qid)
            if question is None or question.status != "open":
                return Executed(False, None, f"la pregunta #{qid} ya no está abierta")
            repo.answer_question(conn, qid, f["answer"], answered_by=0)  # 0 = el cerebro
            task = repo.get_task(conn, question.task_id)
            if task and task.status == "waiting_input" and not repo.list_open_questions(conn, task_id=task.id):
                repo.transition_task(conn, task.id, "queued", expected="waiting_input")
            return Executed(True, f"question:{qid}", f"respondió la pregunta #{qid} de la tarea #{question.task_id}")

        if decision.action == "propose_context":
            ctx = knowledge.propose_context(conn, project.id, f["text"], brain_decision_id=brain_decision_id)
            return Executed(True, f"context:{ctx.id}", f"propuso el contexto #{ctx.id} (queda como borrador)")

        if decision.action == "ask":
            q = repo.add_brain_question(conn, project.id, f["question"], kind=f["kind"],
                                        brain_decision_id=brain_decision_id)
            return Executed(True, f"brain_question:{q.id}", f"te preguntó (#{q.id})", brain_question_id=q.id)
    except RepoError as exc:
        return Executed(False, None, f"no se pudo: {exc}")
    return Executed(False, None, f"acción no ejecutable: {decision.action}")
