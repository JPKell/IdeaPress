"""ideapress.services.stage_reports — what the API and the pages read about a stage or a plan.

Read-only shaping. Kept out of the routes so `GET /projects/{id}/tasks/{task_id}` and the plan page
report the same thing by construction, and so a template never holds a query.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ideapress.errors import ProjectNotFound
from ideapress.infrastructure.db.models import Attempt as AttemptRow
from ideapress.infrastructure.db.models import StageRun as StageRunRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.plan import load_plan, load_requirements

if TYPE_CHECKING:
    from ideapress.services.runtime import Runtime

__all__ = ["plan_report", "task_report", "unit_states"]


def unit_states(runtime: Runtime, project_id: str) -> dict[str, str]:
    """Every unit's key and state, in reading order."""
    with runtime.storage.read() as session:
        rows = session.scalars(
            select(UnitRow).where(UnitRow.project_id == project_id).order_by(UnitRow.ordinal)
        ).all()
        return {row.unit_key: row.state for row in rows}


def task_report(
    runtime: Runtime, *, project_id: str, run_id: str, stage: str | None
) -> dict[str, Any]:
    """Report one stage run: its state, progress, and every attempt it recorded.

    Raises:
        ProjectNotFound: No such run for this project. A run belonging to a different project is
            reported the same way, so the endpoint cannot be used to discover that one exists.
    """
    with runtime.storage.read() as session:
        run = session.get(StageRunRow, run_id)
        if run is None or run.project_id != project_id:
            message = f"No task {run_id!r} for project {project_id!r}."
            raise ProjectNotFound(message, details={"project_id": project_id, "task_id": run_id})
        attempts = session.scalars(
            select(AttemptRow)
            .where(AttemptRow.stage_run_id == run_id)
            .order_by(AttemptRow.created_at)
        ).all()
        units = {
            row.id: row.unit_key
            for row in session.scalars(
                select(UnitRow).where(UnitRow.project_id == project_id)
            ).all()
        }
        return {
            "task_id": run.id,
            "stage": run.stage if stage is None else stage,
            "state": run.state,
            "units_total": run.units_total,
            "units_completed": run.units_completed,
            "units_paused": run.units_paused,
            "started_at": run.started_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "error_code": run.error_code,
            "error_text": run.error_text,
            "backend": run.backend,
            "stream_url": f"/api/v1/projects/{project_id}/tasks/{run.id}/stream",
            "attempts": [
                {
                    "stage": attempt.stage,
                    "unit_key": units.get(attempt.unit_id or ""),
                    "attempt": attempt.attempt,
                    "round": attempt.round,
                    "outcome": attempt.outcome,
                    "backend": attempt.backend,
                    "model_canonical_id": attempt.model_canonical_id,
                    "prompt_id": attempt.prompt_id,
                    "prompt_version": attempt.prompt_version,
                    "input_tokens": attempt.input_tokens,
                    "output_tokens": attempt.output_tokens,
                    "provider_ms": attempt.provider_ms,
                    "degradations": list(attempt.degradations_json),
                    "error_code": attempt.error_code,
                }
                for attempt in attempts
            ],
        }


def plan_report(runtime: Runtime, *, project_id: str) -> dict[str, Any]:
    """Report a project's compiled requirements and unit plan, for the plan page.

    Every requirement is rendered **with its source quote and its checks**, because that pairing is
    the anti-fabrication mitigation: a reviewer must be able to read the claim and its evidence side
    by side, and see which guarantees are mechanical and which are model-assisted (workflows §3).
    """
    project = runtime.projects.get(project_id)
    with runtime.storage.read() as session:
        requirements = load_requirements(session, project_id)
        plan = load_plan(session, project_id)
        states = {
            row.unit_key: row.state
            for row in session.scalars(
                select(UnitRow).where(UnitRow.project_id == project_id)
            ).all()
        }
    return {
        "project": project,
        "requirements": [
            {
                "key": requirement.key,
                "text": requirement.text,
                "blocking": requirement.blocking,
                "source": requirement.source.label,
                "quote": requirement.source.quote,
                "checks": requirement.describe_checks(),
                "mechanical": requirement.is_mechanically_checkable,
                "units": [unit.key for unit in plan.units_for(requirement.key)],
            }
            for requirement in requirements
        ],
        "units": [
            {
                "key": unit.key,
                "title": unit.title,
                "goal": unit.goal_text,
                "requirements": ", ".join(unit.requirement_keys),
                "state": states.get(unit.key, "planned"),
                "target_words": unit.target_words,
            }
            for unit in plan.units
        ],
    }
