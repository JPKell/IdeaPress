"""ideapress.services.stage_bodies — what each stage actually does, wired to the runner.

One function per stage, each taking the runtime and the task. They emit events, record attempts and
move units through the state machine; the runner owns the thread, the terminal state and
cancellation.

The plan stage is here in full. The unit-level stages (`draft` onward) arrive in P4 and P5 and are
registered in the same table, so the runner never grows a branch per stage.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from sqlalchemy import select

from ideapress.errors import StagePreconditionFailed
from ideapress.infrastructure.db.models import StageRun as StageRunRow
from ideapress.infrastructure.db.models import Unit as UnitRow
from ideapress.services.plan import build_plan, store_plan
from ideapress.services.stages import record_attempt

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ideapress.domain.stages import StageId
    from ideapress.services.runtime import Runtime
    from ideapress.services.stages import StageTask

__all__ = ["start_plan", "start_stage"]

logger = logging.getLogger(__name__)


def start_plan(runtime: Runtime, *, project_id: str) -> StageTask:
    """Start the plan stage: compile requirements, then outline units, then gate the plan.

    Raises:
        ProjectNotFound: No such project.
        StageAlreadyRunning: A stage is already running for this project.
        StagePreconditionFailed: The project has no brief, so there is nothing to compile from.
    """
    project = runtime.projects.get(project_id)
    if not project.brief_text.strip():
        message = (
            "This project has no brief, so there is nothing to compile requirements from. Add one "
            "before planning."
        )
        raise StagePreconditionFailed(message, details={"project_id": project_id})

    def body(task: StageTask) -> None:
        runner = runtime.runner
        sink = runtime.events
        database = runtime.storage

        runner.checkpoint(task)
        sink.emit(
            database,
            task.run_id,
            event_type="attempt.started",
            message="compiling requirements",
            data={"stage": "requirements"},
        )
        result = build_plan(
            runner.gateway,
            project_id=project_id,
            brief=project.brief_text,
            sources=None,
            structured_output_tokens=runtime.settings.workflow.structured_output_tokens,
        )
        record_attempt(
            database,
            stage_run_id=task.run_id,
            stage="requirements",
            result=result.compilation.result,
            prompt_id=result.compilation.prompt_id,
            prompt_version=result.compilation.prompt_version,
            prompt_sha256=result.compilation.prompt_sha256,
            outcome="completed",
            store_content=runner.store_content,
        )
        sink.emit(
            database,
            task.run_id,
            event_type="requirements.compiled",
            message=(
                f"{len(result.compilation.requirements)} requirement(s) compiled, "
                f"{len(result.compilation.rejected)} rejected as ungrounded"
            ),
            data={
                "compiled": len(result.compilation.requirements),
                "blocking": len(result.compilation.blocking),
                "rejected": [
                    {"text": r.text, "reason": r.reason} for r in result.compilation.rejected
                ],
            },
        )

        runner.checkpoint(task)
        record_attempt(
            database,
            stage_run_id=task.run_id,
            stage="outline",
            result=result.outline_result,
            prompt_id=result.outline_prompt_id,
            prompt_version=result.outline_prompt_version,
            prompt_sha256=result.outline_prompt_sha256,
            outcome="completed",
            store_content=runner.store_content,
        )
        store_plan(database, project_id=project_id, result=result)
        runtime.projects.update(project_id, status="planning")
        with database.write() as session:
            run = session.get(StageRunRow, task.run_id)
            if run is not None:
                run.units_total = len(result.plan.units)
                run.units_completed = len(result.plan.units)
        sink.emit(
            database,
            task.run_id,
            event_type="plan.built",
            message=f"{len(result.plan.units)} unit(s) planned",
            data={
                "units": [
                    {
                        "key": unit.key,
                        "title": unit.title,
                        "requirements": list(unit.requirement_keys),
                    }
                    for unit in result.plan.units
                ]
            },
        )

    return runtime.runner.start(project_id=project_id, stage="outline", body=body, units_total=0)


def start_stage(
    runtime: Runtime,
    *,
    project_id: str,
    stage: StageId,
    units: Sequence[str] | None = None,
    resume: bool = False,
    overrides: dict[str, Any] | None = None,
) -> StageTask:
    """Start one unit-level stage over a project's units.

    Args:
        runtime: The process's handles.
        project_id: Which project.
        stage: Which stage.
        units: Restrict to these unit keys; all planned units when ``None``.
        resume: Continue from the first incomplete unit rather than starting over.
        overrides: Per-run limits, recorded on the run.

    Returns:
        The running task.

    Raises:
        StagePreconditionFailed: The stage has no body yet, or the project has no plan.
    """
    from ideapress.services.stage_registry import STAGE_BODIES

    runtime.projects.get(project_id)
    database = runtime.storage
    with database.read() as session:
        planned = session.scalars(
            select(UnitRow).where(UnitRow.project_id == project_id).order_by(UnitRow.ordinal)
        ).all()
        keys = [row.unit_key for row in planned]
    if not keys:
        message = (
            "This project has no plan, so there are no units to work on. Run the plan stage first."
        )
        raise StagePreconditionFailed(message, details={"project_id": project_id})

    selected = [key for key in keys if units is None or key in units]
    factory = STAGE_BODIES.get(stage)
    if factory is None:
        message = (
            f"The {stage!r} stage has no implementation in this build. Implemented: "
            f"{', '.join(sorted(STAGE_BODIES))}."
        )
        raise StagePreconditionFailed(message, details={"stage": stage})

    return runtime.runner.start(
        project_id=project_id,
        stage=stage,
        body=factory(runtime, project_id=project_id, unit_keys=selected, resume=resume),
        options=dict(overrides or {}),
        units_total=len(selected),
    )
