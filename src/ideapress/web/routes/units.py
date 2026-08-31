"""ideapress.web.routes.units — a unit's content, its provenance and its history.

Model-produced text reaches the templates as a plain string and is escaped once by the shared
macros. There is no ``| safe`` in this package on anything a model wrote (risk S1).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from mirrorwall import json_response
from pydantic import BaseModel, ConfigDict, Field

# Imported at runtime, not under TYPE_CHECKING: FastAPI reads a handler's return annotation
# when it builds the OpenAPI schema, and a forward reference it cannot resolve makes
# `app.openapi()` raise — which is a 500 on /api/v1/docs that no other test would notice.
from starlette.responses import HTMLResponse, JSONResponse

from ideapress.web.rendering import render

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["units"])
ui_router = APIRouter(include_in_schema=False)


def _reports(request: Request) -> Any:
    from ideapress.services import unit_reports

    return unit_reports


@router.get("/projects/{project_id}/units")
def list_units(request: Request, project_id: str) -> JSONResponse:
    """Unit list with state, version, requirement coverage and the last validation."""
    return json_response(
        {"units": _reports(request).unit_list(request.app.state.runtime, project_id=project_id)}
    )


@router.get("/projects/{project_id}/units/{unit_key}")
def get_unit(request: Request, project_id: str, unit_key: str) -> JSONResponse:
    """One unit's current content plus its full provenance."""
    return json_response(
        _reports(request).unit_detail(
            request.app.state.runtime, project_id=project_id, unit_key=unit_key
        )
    )


@router.get("/projects/{project_id}/units/{unit_key}/history")
def get_unit_history(request: Request, project_id: str, unit_key: str) -> JSONResponse:
    """Every version with its coverage."""
    from ideapress.services.units import unit_history

    with request.app.state.runtime.storage.read() as session:
        return json_response({"versions": unit_history(session, project_id, unit_key)})


class ReviseRequest(BaseModel):
    """``POST /projects/{id}/units/{unit_id}/revise`` body."""

    model_config = ConfigDict(extra="forbid")

    instructions: str = Field(default="", max_length=4000)


@router.post("/projects/{project_id}/units/{unit_key}/revise", status_code=202)
def post_revise(
    request: Request, project_id: str, unit_key: str, body: ReviseRequest
) -> JSONResponse:
    """Revise one unit, bounded by the same limits as any other revision.

    A committed unit is immutable, so this creates a **new version** rather than editing one
    (data model §3's `committed -> revising` arrow). The instructions are the user's, and they are
    carried into the context as findings — they do not change the bounds, which stay Python's.

    Raises:
        UnitNotFound: No such unit.
        StagePreconditionFailed: The unit has no committed version to revise.
        StageAlreadyRunning: A stage is already running for this project.
    """
    from ideapress.services.stage_bodies import start_stage

    runtime = request.app.state.runtime
    detail = _reports(request).unit_detail(runtime, project_id=project_id, unit_key=unit_key)
    if detail["version"] is None:
        from ideapress.errors import StagePreconditionFailed

        message = (
            f"Unit {unit_key} has no committed version to revise. Draft it first; a revision "
            "creates a new version of something that exists."
        )
        raise StagePreconditionFailed(
            message, details={"unit_key": unit_key, "state": detail["state"]}
        )
    task = start_stage(
        runtime,
        project_id=project_id,
        stage="draft",
        units=[unit_key],
        overrides={"instructions": body.instructions} if body.instructions else {},
    )
    return json_response(
        {
            "task_id": task.run_id,
            "unit_key": unit_key,
            "stage": task.stage,
            "stream_url": f"/api/v1/projects/{project_id}/tasks/{task.run_id}/stream",
        },
        status=202,
    )


@ui_router.get("/projects/{project_id}/units/{unit_key}")
def unit_page(request: Request, project_id: str, unit_key: str) -> HTMLResponse:
    """Render a unit: its content, validation report, coverage and history."""
    detail = _reports(request).unit_detail(
        request.app.state.runtime, project_id=project_id, unit_key=unit_key
    )
    return HTMLResponse(render("units/detail.html", page="projects", page_title=unit_key, **detail))
