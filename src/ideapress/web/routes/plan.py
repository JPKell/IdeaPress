"""ideapress.web.routes.plan — the plan, over the API and as a page.

Separate from `routes/stages.py`, which starts and follows stage *runs*: this serves what the plan
stage produced, and it is what a person reads to judge whether the requirements are real. Every
requirement appears with the quotation that supports it, because that pairing is the mitigation
risk T6 actually rests on.

The API half (row WP5) is the page's own data and the plan editor's one operation, so a client
that cannot reach this page — WeightRoomGym, over the LAN — reads and edits the same plan by the
same rules, and is refused by the same gate.
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

from ideapress.web.csrf import render_form_page

__all__ = ["router", "ui_router"]

router = APIRouter(tags=["plan"])
ui_router = APIRouter(include_in_schema=False)


class PlanEditRequest(BaseModel):
    """``POST /projects/{id}/plan/edits`` body: one edit, as the plan page's forms send it."""

    model_config = ConfigDict(extra="forbid")

    operation: str = Field(description="reorder, split, merge, reassign or goal")
    unit_keys: list[str] = Field(default_factory=list)
    requirement_keys: list[str] = Field(default_factory=list)
    text: str = Field(default="", max_length=2000)
    position: int | None = None


def _plan(request: Request, project_id: str) -> dict[str, Any]:
    """The plan report as the API answers it: everything but the project object."""
    from ideapress.services.plan_editing import EDITABLE_STATES
    from ideapress.services.stage_reports import plan_report

    report = plan_report(request.app.state.runtime, project_id=project_id)
    return {
        "project_id": project_id,
        "requirements": report["requirements"],
        "units": report["units"],
        "editable_states": sorted(EDITABLE_STATES),
    }


def _keys(values: list[str]) -> tuple[str, ...]:
    """Keys with blanks dropped, as the page's comma-separated fields are read."""
    return tuple(value.strip() for value in values if value.strip())


@router.get("/projects/{project_id}/plan")
def get_plan(request: Request, project_id: str) -> JSONResponse:
    """The compiled requirements, each with its source quote and checks, and the unit plan.

    Raises:
        ProjectNotFound: No such project.
    """
    return json_response(_plan(request, project_id))


@router.post("/projects/{project_id}/plan/edits")
def post_plan_edit(request: Request, project_id: str, body: PlanEditRequest) -> JSONResponse:
    """Apply one plan edit, re-check the whole plan, and answer the plan as stored.

    Raises:
        ValidationError: ``400``, and the plan is unchanged: the edit is malformed, it would leave
            a blocking requirement with no unit (``details.unassigned_requirement_keys``), or it
            would renumber a unit holding committed or in-flight text
            (``details.protected_unit_keys``).
        ProjectNotFound: No such project.
    """
    from ideapress.services.plan_editing import PlanEdit, apply_edit

    edit = PlanEdit(
        operation=body.operation,
        unit_keys=_keys(body.unit_keys),
        requirement_keys=_keys(body.requirement_keys),
        text=body.text,
        position=body.position,
    )
    apply_edit(request.app.state.runtime, project_id=project_id, edit=edit)
    return json_response(_plan(request, project_id))


@ui_router.get("/projects/{project_id}/plan")
def plan_page(request: Request, project_id: str) -> HTMLResponse:
    """Render the compiled requirements with their sources, and the unit plan."""
    from ideapress.services.stage_reports import plan_report

    report = plan_report(request.app.state.runtime, project_id=project_id)
    # `render_form_page` rather than `render`: the page now carries the plan editor's forms, and a
    # form without the double-submit token is refused by MirrorWall's CSRF middleware (ADR-0026 §2).
    return render_form_page(
        request,
        "plan/index.html",
        page="projects",
        page_title="Plan",
        # Defaults rather than a `is defined` guard in the template: the environment runs under
        # `StrictUndefined`, and keeping it strict is what makes a mistyped variable a loud error
        # instead of a silently blank panel.
        edit_refusal="",
        edit_refusal_details={},
        **report,
    )
