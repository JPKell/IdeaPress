"""ideapress.web.routes.plan — the plan page.

Separate from `routes/stages.py`, which starts and follows stage *runs*: this renders what the plan
stage produced, and it is the page a person reads to judge whether the requirements are real. Every
requirement appears with the quotation that supports it, because that pairing is the mitigation
risk T6 actually rests on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from starlette.responses import HTMLResponse

from ideapress.web.csrf import render_form_page

if TYPE_CHECKING:
    pass

__all__ = ["ui_router"]

ui_router = APIRouter(include_in_schema=False)


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
