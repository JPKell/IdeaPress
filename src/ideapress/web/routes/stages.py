"""ideapress.web.routes.stages — starting stages, following them, and cancelling them.

Route handlers contain no business logic: each starts or reads one thing. The SSE handler is the
only ``async def`` here, and it issues no query on the event loop — MirrorWall's ``sse_response``
dispatches every read into the worker threadpool (ADR-0003 §6–8).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request, status
from mirrorwall import json_response
from pydantic import BaseModel, ConfigDict, Field
from setspec import GeneratorInfo

# Imported at runtime, not under TYPE_CHECKING: FastAPI reads a handler's return annotation
# when it builds the OpenAPI schema, and a forward reference it cannot resolve makes
# `app.openapi()` raise — which is a 500 on /api/v1/docs that no other test would notice.
from starlette.responses import JSONResponse, StreamingResponse

from ideapress.__about__ import __version__
from ideapress.domain.stages import STAGES, is_stage
from ideapress.errors import StagePreconditionFailed
from ideapress.services.events import TERMINAL_STAGE_EVENTS

if TYPE_CHECKING:
    from ideapress.domain.stages import StageId

__all__ = ["router"]

router = APIRouter(tags=["stages"])

GENERATOR = GeneratorInfo(name="ideapress", version=__version__)


class StageRunRequest(BaseModel):
    """``POST /projects/{id}/stages/{stage}/run`` body."""

    model_config = ConfigDict(extra="forbid")

    units: list[str] | None = None
    resume: bool = False
    overrides: dict[str, Any] = Field(default_factory=dict)


def _runtime(request: Request) -> Any:
    return request.app.state.runtime


def _task_payload(request: Request, run_id: str, project_id: str, stage: str) -> dict[str, Any]:
    from ideapress.services.stage_reports import task_report

    return task_report(_runtime(request), project_id=project_id, run_id=run_id, stage=stage)


@router.post("/projects/{project_id}/plan", status_code=status.HTTP_202_ACCEPTED)
def post_plan(request: Request, project_id: str) -> JSONResponse:
    """Compile requirements and build the unit plan. Returns the task.

    Raises:
        ProjectNotFound: No such project.
        StageAlreadyRunning: A stage is already running for this project.
    """
    from ideapress.services.stage_bodies import start_plan

    task = start_plan(_runtime(request), project_id=project_id)
    return json_response(
        _task_payload(request, task.run_id, project_id, task.stage),
        status=status.HTTP_202_ACCEPTED,
    )


@router.post("/projects/{project_id}/stages/{stage}/run", status_code=status.HTTP_202_ACCEPTED)
def post_stage_run(
    request: Request, project_id: str, stage: str, body: StageRunRequest
) -> JSONResponse:
    """Start one stage over the project's units.

    Raises:
        StagePreconditionFailed: ``stage`` is not a stage in workflows §2, or the project is not in
            a state that stage can run from.
        StageAlreadyRunning: A stage is already running for this project.
    """
    from ideapress.services.stage_bodies import start_stage

    if not is_stage(stage):
        message = f"{stage!r} is not a stage. The stages are: {', '.join(sorted(STAGES))}."
        raise StagePreconditionFailed(message, details={"stage": stage})
    task = start_stage(
        _runtime(request),
        project_id=project_id,
        # `is_stage` above narrowed this from a path segment to a stage identifier; the Literal is
        # not something a static checker can read out of a runtime membership test.
        stage=cast("StageId", stage),
        units=body.units,
        resume=body.resume,
        overrides=body.overrides,
    )
    return json_response(
        _task_payload(request, task.run_id, project_id, stage), status=status.HTTP_202_ACCEPTED
    )


@router.get("/projects/{project_id}/tasks/{task_id}")
def get_task(request: Request, project_id: str, task_id: str) -> JSONResponse:
    """Task state, per-unit progress, attempts and degradations."""
    from ideapress.services.stage_reports import task_report

    return json_response(
        task_report(_runtime(request), project_id=project_id, run_id=task_id, stage=None)
    )


@router.get("/projects/{project_id}/tasks/{task_id}/stream")
async def get_task_stream(request: Request, project_id: str, task_id: str) -> StreamingResponse:
    """SSE: the stage's events, replayed from ``Last-Event-ID`` and then followed live.

    ``async def`` because it holds a connection open; every read into the event store is dispatched
    to the worker threadpool by ``sse_response``, so no query runs on the event loop.
    """
    from mirrorwall import sse_response

    runtime = _runtime(request)
    return sse_response(
        runtime.events.source(runtime.storage, task_id),
        stream_id=task_id,
        last_event_id=request.headers.get("last-event-id"),
        generator=GENERATOR,
        heartbeat_seconds=15.0,
        poll_interval_seconds=0.05,
        terminal_events=TERMINAL_STAGE_EVENTS,
    )


@router.post("/projects/{project_id}/tasks/{task_id}/cancel", status_code=status.HTTP_202_ACCEPTED)
def post_cancel(request: Request, project_id: str, task_id: str) -> JSONResponse:
    """Cancel a stage. Honoured at the next model-call boundary; idempotent."""
    cancelled = _runtime(request).runner.cancel(task_id)
    return json_response({"task_id": task_id, "cancelling": cancelled}, status=202)


@router.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> JSONResponse:
    """One workflow definition.

    Raises:
        StagePreconditionFailed: No such workflow. One ships at 1.0; a second is what makes the
            content-type registry earn its keep, and this endpoint exists so the shape is settled
            before there is one.
    """
    if workflow_id != "standard":
        message = f"{workflow_id!r} is not a workflow. This build ships: standard."
        raise StagePreconditionFailed(message, details={"workflow_id": workflow_id})
    return json_response(_workflow_definition())


def _workflow_definition() -> dict[str, Any]:
    """The one workflow, from the stage table rather than from a second list of stages."""
    return {
        "id": "standard",
        "version": "1.0",
        "stages": [
            {
                "stage": definition.stage,
                "ordinal": definition.ordinal,
                "uses_model": definition.uses_model,
                "gate": definition.gate,
            }
            for definition in STAGES.values()
        ],
    }


@router.get("/workflows")
def list_workflows() -> JSONResponse:
    """The workflow definitions: stage order, gates, and which stages use a model."""
    return json_response({"workflows": [_workflow_definition()]})
